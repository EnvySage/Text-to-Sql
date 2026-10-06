# 2.4 执行反馈循环（工具调用）设计

> 状态：待评审。评审通过后实现，实现完把定型部分折回 `docs/DESIGN.md`、`docs/ROADMAP.md`。
> 上位文档：`docs/DESIGN.md` 3.3（agent/ 模块表）、4.4（事件流）、`docs/ROADMAP.md` 2.4。

## 1. 目标

把评测链路里的**单次生成**换成**工具循环**：模型可以用工具试跑 SQL、看真实结果，
自己决定验证或修正，直到交出最终 SQL。

**这是搭台子，不是冲数字。** 2.4 的直接靶子（SQL 跑不通 + 没吐出 SQL）只有约 5 题（2%），
低于 D19 的噪声门槛，跑 250 题大概率测不显著。价值在于 `agent/` 的事件流和循环——
planner、verifier、CLI、Web 将来都挂在这套东西上。

## 2. 现状

```
run_one:  schema_text → generate_sql(单次) → sandbox.run → result_match
```

`agent/` 目前只有 `baseline.py`、`baseline_dialect.py`、`schema.py`。
`llm/` 的工具体系（`ToolSpec`/`ToolCall`/`ToolResult`、`openai_compat` 的序列化）已就绪，
但从未接过真实模型。2026-10-06 的 spike 验证：`global:deepseek-v4.1-flash` 会工具调用，
两轮循环（调工具 → 回灌结果 → 收工）跑通。

## 3. 设计

### 3.1 新模块

| 文件 | 职责 |
|---|---|
| `agent/events.py` | `AgentEvent` 类型（DESIGN 4.4 定义的子集） |
| `agent/tools.py` | 工具定义（`ToolSpec`）+ 工具调用分派到 sandbox |
| `agent/core.py` | 循环本体，产出事件流 |

三个都只依赖 `llm/` 和 `sandbox/` 的接口，不 import 任何厂商库，不依赖 `eval/`。

### 3.2 工具

只给两个。理由见第 7 节。

**`execute_sql(sql)`** —— 试跑，结果回给模型。

```python
ToolSpec(
    name="execute_sql",
    description="在数据库上执行一条只读 SELECT 查询，返回结果行。用于验证查询是否正确。",
    parameters={"sql": "要执行的 SELECT 语句"},
)
```

**`submit_sql(sql)`** —— 交卷，结束循环，不回结果。

```python
ToolSpec(
    name="submit_sql",
    description="提交你的最终 SQL 答案，结束本轮任务。",
    parameters={"sql": "最终的 SELECT 语句"},
)
```

为什么要有 `submit_sql`：循环需要知道模型"答完了"。没有它，只能靠"模型没调工具、只回文字"
来猜，再从文字里用 `extract_sql` 抠——而模型探索几轮后，那段文字里**可能提到多条试过的 SQL**，
抠到作废的那条就会误判。`submit_sql` 让交卷成为**明确动作**，最终 SQL 是**工具参数**，不用猜。

兜底：模型若没用 `submit_sql` 就自己停了，仍用 `baseline.extract_sql` 从文字里抠（保持现有行为）。

### 3.3 循环

```python
def run(question, *, provider, sandbox, schema, dialect, evidence="",
        max_steps=10, max_tokens=8192) -> Iterator[AgentEvent]:
    messages = [Message.user(USER_TEMPLATE.format(...))]   # schema + evidence + 问题 + 工具说明
    usage_total = Usage()
    for step in range(1, max_steps + 1):
        yield AgentEvent("step_start", {"step": step})
        resp = provider.chat(system=system_prompt(dialect), messages=messages,
                             tools=TOOLS, max_tokens=max_tokens)
        usage_total = usage_total + resp.usage
        if not resp.tool_calls:
            # 模型没调工具就停了：走兜底，从文字里抠
            yield AgentEvent("final", {..., "sql": extract_sql(resp.text), "hit_cap": False})
            return
        messages.append(resp.to_message())
        results = []
        for tc in resp.tool_calls:
            if tc.name == "submit_sql":
                yield AgentEvent("final", {..., "sql": tc.args["sql"], "hit_cap": False})
                return
            res = sandbox.run(tc.args["sql"])           # 走同一个沙箱、同一道防线
            yield AgentEvent("tool_call", {...}); yield AgentEvent("tool_result", {...})
            results.append(ToolResult(call_id=tc.id, content=format_result(res), is_error=not res.ok))
        messages.append(Message.results(results))
    yield AgentEvent("final", {..., "sql": "", "hit_cap": True})   # 撞上限 = 没收敛
```

**`max_steps=10` 是防跑飞的兜底，不是预算。** 正常模型 1-3 步就交卷；撞到 10 说明它绕不出来，
`hit_cap=True` 记下来——这是异常信号，不是"用满了额度"。

**与 DESIGN 4.4 的差异**：4.4 的草案签名是 `run(question, db_path) -> Iterator[AgentEvent]`。
实际签名多带 `provider` / `sandbox` / `schema` / `dialect`——因为 schema 的加载和沙箱的选择由调用方
（runner）负责，循环不该自己去开连接。实现后同步更新 DESIGN 4.4。

### 3.4 关键约束

1. **安全零新增**：工具执行的 SQL 走**同一个 `sandbox.run()`**——guard AST 白名单 + 只读连接，
   两道防线原样。模型调工具 = 走一遍已有防线，没有新攻击面。
2. **工具结果必须截断**：`ExecResult` 最多 2000 行，全塞给模型会爆 token。格式化时只给
   **前 20 行 + 总行数**（`"共 342 行，前 20 行：…"`）。超时/报错原文照给（模型要靠它改）。
3. **usage 聚合**：一次提问可能 N 次调用，用 `Usage.__add__` 相加（它已做计价单位校验）。
4. **失败收敛**：模型调用异常（`LLMError`）收敛成 `error` 事件，**不抛到评测主循环**
   （项目硬规矩：单题炸掉不能中断整轮）。
5. **prompt 要跟方言走**：`core.system_prompt(dialect)` =
   `baseline_dialect.system_prompt(dialect) + TOOL_SUFFIX`。不能直接用 `baseline.SYSTEM`——
   它写死"你是一个 SQLite 专家"，在 PG 上跑会告诉模型错误的方言，**不报错只会静默写错 SQL**。
   `baseline_dialect.system_prompt("sqlite") == baseline.SYSTEM`，所以 SQLite 上的 prompt
   和常量版逐字节相同，已有数字的可比性不受影响。`TOOL_SUFFIX` 是常量：工具说明对各方言
   必须一致，否则跨方言对比会多出一个变量。

### 3.5 事件类型（2.4 用到的子集）

`AgentEvent(type, payload, usage=None, ts=...)`，类型用：

| type | payload |
|---|---|
| `step_start` | `{step}` |
| `tool_call` | `{name, args}` |
| `tool_result` | `{name, ok, rows, error}` |
| `final` | `{sql, steps, tool_calls, hit_cap, usage}` |
| `error` | `{message, steps, tool_calls}` |

`plan` / `verify` / `retry` 留给阶段 3。DESIGN 4.4 的类型表是全集，这里先实现子集
（`EventType` 声明 8 种，2.4 实际产出 5 种）。

`error` 也带 `steps` / `tool_calls`：循环跑到第 7 步崩掉时，消费方要能看出前面已经走了几步、
调过几次工具。否则这两个字段在 error 路径上结构性恒为 0，读它的人会被误导。

### 3.6 runner 接线

`run_one` 里多一个 `--tools` 开关（默认关）：

```python
if use_tools:
    outcome = consume(core.run(question, provider=..., sandbox=sandbox, schema=schema,
                               dialect=sandbox.dialect, ...))
    gen_sql, usage = outcome.sql, outcome.usage
else:
    gen = generate_sql(...)     # 现有单次路径，一字不动
```

`dialect` 从 `sandbox.dialect` 取，不另设开关：同一个沙箱跑的 SQL 和 prompt 里的方言名
必须同源，分成两处配置迟早会对不上。

**默认关 → 老路不变，已有数字不受影响。** 这是硬要求。

### 3.7 Record 新增字段

| 字段 | 意思 |
|---|---|
| `steps` | 循环了几步 |
| `tool_calls` | 一共调了几次工具 |
| `hit_cap` | 是否撞到 `max_steps`（`True` = 没收敛，异常） |

用于事后算"平均每题几次调用"、"多少题绕不出来"。**注意口径**：调用次数只有在
**准确率相当的前提下**比才有意义——单看"次数少"会把"懒得验证"误判成"聪明"。

## 4. 对照口径

2.4 vs colvalues 是 **bundle**：prompt 措辞（加了工具说明）和工具可用性一起变，
不是单变量。和 2.2 一样属于"一个能力"。写进 EVAL 时必须注明。

## 5. 测试（先写测试）

用假 provider + 假 sandbox，不发网络、不碰数据库：

1. 模型调 `execute_sql` → 回灌 → 调 `submit_sql` → 拿到最终 SQL
2. 模型直接 `submit_sql`（零次试跑）→ 一步收工
3. 模型没调工具、只回文字 → 兜底 `extract_sql`
4. 模型连续调工具到 `max_steps` → `hit_cap=True`，不抛异常
5. `execute_sql` 报错 → `tool_result.is_error=True`，错误原文回灌
6. usage 跨多次调用正确相加
7. provider 抛 `LLMError` → 收敛成 `error` 事件，不冒泡
8. runner `--tools` 关时，请求与现有单次路径逐字节一致

## 6. 不在范围

- **不跑 250 题正式评测**：目标 5 题低于噪声门槛，ROI 差。先把架构建好、跑通 6 题 mini 自检；
  正式数字留到阶段 3 挂上 planner / verifier 再测。
- 不加 `get_schema` / `sample_rows` / `search_column` 等工具：schema 整份已在 prompt 里，
  取数用 `SELECT ... LIMIT` 自己就办到。**工具由错误数据逼出来，不拍脑袋想。** 跑完再看。
- 不碰 Python 执行沙箱（3.2）：那是能跑任意代码的危险活，要单独设计安全模型。

## 7. 为什么只给两个工具

`execute_sql` 对只读 SQL agent 是万能钥匙——"看几行数据"（`SELECT * LIMIT 5`）、
"确认某列存在"（schema 已在 prompt 里）都能用它办到。替模型包一层反而更差：
多一个工具要维护、模型要学、还可能浪费轮次。`submit_sql` 是唯一不被 `execute_sql`
涵盖的——它不是"能力"，是"交卷方式"。
