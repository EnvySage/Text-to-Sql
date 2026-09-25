# 设计文档

> 本文档是架构层面的唯一事实来源。代码和本文档冲突时，要么改代码，要么先改本文档再改代码——
> 不允许两者长期不一致。评测口径见 [EVAL.md](EVAL.md)，阶段计划见 [ROADMAP.md](ROADMAP.md)，
> 每个关键取舍的来龙去脉见 [DECISIONS.md](DECISIONS.md)。

---

## 1. 目标与非目标

### 目标

做一个**自主数据分析 Agent**：接收模糊的业务问题，自己拆解、查询、校验、给出有证据链的结论。

项目作为秋招作品集，核心价值排序：

1. **可量化**：每个模块的收益都有同一评测集上的对照数字
2. **可追问**：每个设计取舍都说得出为什么，经得起面试追问三层
3. **能跑**：有可演示的 CLI 和 Web 界面

### 非目标

以下明确不做，出现相关需求时先回到这里：

- 数据写入（INSERT/UPDATE/DELETE）——系统只读
- 跨库联查（一条查询同时连多个库）、实时流数据。注意：**支持多种数据库类型**不在此列，
  每次分析只连一个库，但这个库可以是 SQLite / PostgreSQL / MySQL，见 3.2
- 权限系统、多租户
- 复杂的多轮对话上下文管理
- 通用 agent 框架——只服务数据分析这一个场景
- 追求 BIRD 排行榜名次——准确率是手段，消融曲线和成本工程才是产出

---

## 2. 总体架构

```
                 ┌──────────────┐     ┌──────────────┐
   用户问题 ───► │  CLI (规划)   │     │ Streamlit(规划)│   ← 界面层：只渲染，不含逻辑
                 └──────┬───────┘     └──────┬───────┘
                        │   订阅 AgentEvent 事件流  │
                        ▼                      ▼
                 ┌─────────────────────────────────────┐
                 │            agent/  主循环             │   ← 编排层
                 │  planner → sql_gen → 执行 → verifier  │
                 └───┬──────────┬──────────┬───────────┘
                     │          │          │
          ┌──────────▼──┐ ┌─────▼──────┐ ┌─▼─────────────┐
          │   llm/       │ │ sandbox/   │ │ retrieval/    │   ← 能力层
          │ 厂商抽象+路由 │ │ 安全执行    │ │ schema/样例检索 │
          └──────────────┘ └────────────┘ └───────────────┘

          ┌───────────────────────────────────────────────┐
          │ eval/  评测框架：驱动 agent 跑数据集、比对、归因  │   ← 旁路：衡量上面所有东西
          └───────────────────────────────────────────────┘
```

依赖方向**只能自上而下**。能力层之间互不依赖；`eval/` 可以依赖任何层，任何层不得依赖 `eval/`。

---

## 3. 模块职责

状态说明：✅ 已实现并有测试　🚧 部分实现　📋 规划中

### 3.1 `llm/` ✅ 厂商抽象层

**职责**：把各家 LLM API 的差异吞掉，对上只暴露一套中立类型。

| 文件 | 职责 |
|---|---|
| `base.py` | 中立类型：`Message` `ToolCall` `ToolResult` `ToolSpec` `Usage` `LLMResponse` `LLMProvider` |
| `openai_compat.py` | OpenAI 兼容接口（本地网关、DeepSeek、OpenAI、Kimi 等），含退避重试 |
| `anthropic.py` | Anthropic Messages API（已写好，暂未启用） |
| `router.py` | 按**角色**取 provider，缺 key 降级，读取角色级参数 |
| `cost.py` | 计价：优先用接口上报的实际扣费，价格表兜底 |

**边界规则**：`llm/` 之外的代码**禁止** `import openai` / `import anthropic`，禁止构造厂商原生消息格式。

### 3.2 `sandbox/` ✅ 安全执行

| 文件 | 状态 | 职责 |
|---|---|---|
| `__init__.py` | ✅ | `open_sandbox(连接目标)`：按连接地址选实现；`dialect_of()` |
| `base.py` | ✅ | `Sandbox` 协议、`ExecResult`；`BaseSandbox` 公共流程（guard → 补 LIMIT → 截断检测） |
| `guard.py` | ✅ | sqlglot 按方言解析 AST，白名单只放行单条 SELECT/WITH/集合查询；拒绝 `SELECT INTO`、行锁、危险函数；补 LIMIT |
| `executor.py` | ✅ | `SQLiteSandbox`：只读连接执行，超时中断，行数上限，失败返回而不抛异常 |
| `postgres.py` | ✅ | `PostgresSandbox`：账号自检、只读事务、服务端游标、服务端超时 |
| `mysql.py` | 📋 | `MySQLSandbox`，需要引入 pymysql（待用户同意） |

两道防线相互独立：AST 白名单挡语义，连接权限挡驱动层写入。任何一道都不能因为"另一道已经挡了"而被删掉。

**多数据库**：一个 `Sandbox` 接口，每种库一个实现，按连接地址选。上层（agent、eval）只认接口。
每种库的两道防线：

| 方言 | 第一道：guard | 第二道：连接 | 超时 |
|---|---|---|---|
| SQLite ✅ | AST 白名单 + 危险函数 | `mode=ro` URI | 另起线程调 `interrupt()` |
| PostgreSQL ✅ | 同左 | **账号自检**（超级用户 / 任一表可写 / 服务器文件角色 → 拒绝执行）+ 只读事务 + 服务端游标 | 服务端 `statement_timeout` |
| MySQL 📋 | 同左 | **只读账号** + `SET SESSION TRANSACTION READ ONLY` | 服务端 `MAX_EXECUTION_TIME` |

- 服务端库上，和 `mode=ro` 同等强度的是**账号权限**。会话级只读能被一条 `SET` 改回去，只能算补充。
- 在服务端库上，第二道防线必须有，不能只当备用：PG/MySQL 的自定义函数可以带写副作用，AST 看不出来，
  函数黑名单不可能列全。
- 危险函数黑名单按方言分组，但**不论传入哪个方言都全部生效**：dialect 传错时不至于留出漏洞，
  这些函数名在别的方言里也不是合法的分析函数，没有误伤成本。
- `dialect` 必须从 sandbox 一路传到 guard：`ensure_limit` 会重新生成 SQL，按错的方言生成时，
  MySQL 的 `` `order id` `` 会变成 `"order id"`——在 MySQL 里这是字符串常量，查询照跑，结果静默出错。
- "guard → 补 LIMIT → 多取一行判截断"在 `BaseSandbox` 里只写一份；子类只实现 `_fetch`（连库、限时、翻译报错）。
- PG 的只读事务能被 `SET TRANSACTION READ WRITE` 改回去（实测），所以账号自检不能省；
  服务端游标（`DECLARE CURSOR` 只接受 SELECT/VALUES）让写语句在语法层就进不来，同时保证只取 n 行。
- PG 集成测试库：`tests/pg/docker-compose.yml`，数据放 tmpfs，端口只绑本机；没起库时测试自动跳过。

📋 规划：Python 代码执行沙箱（第 3 阶段，给统计计算用），安全模型需单独设计后再实现。

### 3.3 `agent/` 🚧 编排层

| 文件 | 状态 | 职责 |
|---|---|---|
| `schema.py` | ✅ | 抽取表结构渲染成 DDL 文本，可选附列描述。SQLite 用 `PRAGMA`；PG 用 `pg_catalog`，表名列名由 `quote_ident` 按需加引号；MySQL 📋 |
| `baseline.py` | ✅ | 对照组：单次生成，不给工具、不看结果、不重试 |
| `baseline_dialect.py` | ✅ | baseline 的方言版：system prompt 只把"SQLite"换成目标方言名；SQLite 时直接调 baseline |
| `events.py` | 📋 | `AgentEvent` 事件类型定义 |
| `core.py` | 📋 | 主循环，产出事件流 |
| `tools.py` | 📋 | 暴露给模型的工具（执行 SQL、查看表结构等） |
| `cli.py` | 📋 | 命令行渲染 |

**baseline.py 冻结规则**：它是所有消融实验的对照组，行为一旦改变，之前所有对比数字作废。
除修复明确的 bug 外不得修改；修改时必须重跑 baseline 并在 EVAL.md 登记。

### 3.4 `retrieval/` 🚧 检索层

| 文件 | 状态 | 职责 |
|---|---|---|
| `descriptions.py` | ✅ | 读 BIRD 的 `database_description/<表>.csv`，得到 `{(表, 列): 说明}`，由 `agent/schema.py` 以 `-- 注释` 形式拼在每列后面 |

📋 规划：样例值（2.2）、schema 裁剪（2.5）、few-shot 召回。

列描述的取舍（见 D17）：列全名、列说明与列名重复时不写；取值说明去掉 `commonsense evidence:` 前缀，
`Not useful` 丢弃；不截断。表名、列名按"小写、去首尾空白和引号、合并空白"匹配。

### 3.5 `trace/` 📋 轨迹存储

⚠️ **包名需要改**：`trace` 和 Python 标准库模块同名，会遮蔽 `import trace`。
动手写这个模块前先改名为 `tracing/`。见 [ROADMAP.md 待决事项](ROADMAP.md#待决事项)。

### 3.6 `eval/` ✅ 评测框架

| 文件 | 职责 |
|---|---|
| `dataset.py` | 加载 BIRD 格式数据集，固定种子随机抽样；`Item.db` 是 SQLite 文件或 PG 连接地址 |
| `metrics.py` | 结果集比对，判断题目是否要求有序 |
| `runner.py` | 批量评测、汇总、失败归因、结果落盘；`--pg-dsn` 时在 PostgreSQL 上跑 |
| `pg/` | Mini-Dev 的 PG 评测库：docker compose + 初始化脚本（按库拆 schema、只读账号） |
| `datasets/mini/` | 6 题微型数据集，用于验证管线本身 |

口径细节全部在 [EVAL.md](EVAL.md)。

---

## 4. 核心接口

### 4.1 LLM 调用

```python
class LLMProvider(Protocol):
    name: str
    model: str
    def chat(self, *, system: str, messages: list[Message],
             tools: list[ToolSpec] | None = None,
             max_tokens: int = 4096, temperature: float = 0.0) -> LLMResponse: ...
```

agent 永远通过 `Router.for_role(role)` 拿 provider，**不直接写模型名**。

### 4.2 Usage 的三条不变量

1. `cached_input_tokens ⊆ input_tokens`——Anthropic 口径不同，适配层负责折算
2. `reasoning_tokens ⊆ output_tokens`
3. `cost_unit` 不同的两个 `Usage` 相加必须抛异常，不得静默合并

### 4.3 SQL 执行

```python
class Sandbox(Protocol):
    dialect: str          # sqlglot 方言名："sqlite" / "postgres" / "mysql"
    def run(self, sql: str, *, enforce_limit: bool = True) -> ExecResult: ...

open_sandbox("path/to/x.sqlite" | "postgresql://...", timeout=30.0, max_rows=2000) -> Sandbox
SQLiteSandbox(db_path, timeout=30.0, max_rows=2000).run(sql) -> ExecResult
PostgresSandbox(dsn, timeout=30.0, max_rows=2000).run(sql) -> ExecResult
```

`ExecResult.ok == False` 时 `error` 里是给模型看的原文，用于执行反馈重试。**执行失败不抛异常。**
新代码（`agent/tools.py` 等）依赖 `Sandbox` 协议，不依赖具体实现；`eval/runner.py` 目前直接用
`SQLiteSandbox`，因为 BIRD 只有 SQLite 版本。

### 4.4 事件流（📋 规划，实现时以此为准）

```python
@dataclass
class AgentEvent:
    type: Literal["plan", "step_start", "tool_call", "tool_result",
                  "retry", "verify", "final", "error"]
    payload: dict
    usage: Usage | None = None
    ts: float = field(default_factory=time.time)

def run(question: str, db_path: Path) -> Iterator[AgentEvent]: ...
```

主循环只产出事件，不做任何渲染。CLI、Web、轨迹存储、评测 runner 都是事件流的消费者。
这样一套引擎可以同时被人看、被存、被回放、被评测。

---

## 5. 数据流（当前 baseline）

```
Item(question, evidence, db)                db：SQLite 文件，或带 search_path 的 PG 连接地址
  → sandbox = open_sandbox(db)               按连接目标选实现
  → schema_text(db)                          整库 DDL
  → generate_sql(provider, dialect, ...)     单次调用；SQLite 时就是 baseline.generate_sql
  → extract_sql(回复文本)                     取不到就记失败，绝不猜
  → sandbox.run(pred_sql)                    guard → 只读执行
  → sandbox.run(gold_sql)                    标准答案也要真跑
  → result_match(pred, gold, order_sensitive=needs_order(question))
  → Record → JSONL
```

---

## 6. 配置

| 文件 | 内容 |
|---|---|
| `.env` | `LOCAL_API_KEY` `LOCAL_BASE_URL` `LOCAL_MODEL`，不进 git |
| `config/routing.yaml` | 角色 → provider/model/max_tokens/params 映射 |
| `config/pricing.yaml` | 兜底价格表，**数字未核实**，网关有上报时不使用 |

当前所有角色都钉在 `cn:deepseek-v4.1-flash`。换模型、开关思考都只改 `routing.yaml`，不改代码。

---

## 7. 横切约束

- **失败收敛**：单道题、单次调用的任何异常都收敛成一条失败记录，不得中断整轮评测
- **可复现**：评测结果必须能由 `(数据集, limit, seed, 模型, 配置, 口径版本)` 完全确定
- **测试先行**：比对逻辑、安全闸门这类"错了会静默产出假数字"的代码，先写测试再上真实数据
- **注释中文**，标识符英文
- **不引入 agent 框架**（LangChain、LlamaIndex 等），理由见 DECISIONS.md
