# 自主数据分析 Agent

在 BIRD benchmark 上做 text-to-SQL，逐步演进成能做多步分析、能自我校验的数据分析 agent。

项目的重点不是"能跑通"，而是**每一步改动都有可复现的量化证据**。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | 架构、模块边界、核心接口 |
| [docs/EVAL.md](docs/EVAL.md) | 评测协议、比对口径、实验登记表 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 阶段计划、完成标准、待决事项 |
| [docs/DECISIONS.md](docs/DECISIONS.md) | 关键决策的背景和代价 |
| [CLAUDE.md](CLAUDE.md) | AI 协作约定 |

本 README 是概览；细节以 `docs/` 为准，两者冲突时改 README。

---

## 当前状态

已完成第 1 周地基：provider 抽象层、SQL 安全沙箱、评测框架、baseline。

```
llm/          与厂商无关的 LLM 抽象层（Anthropic / OpenAI 兼容双适配 + 路由 + 计价）
sandbox/      SQL 安全闸门（sqlglot AST 白名单）+ 只读执行器
agent/        schema 抽取、baseline 生成器
eval/         数据集加载、结果集比对、批量评测
tests/        44 个测试，无需 API key 和网络
```

## 快速开始

```bash
cp .env.example .env        # 填入 LOCAL_API_KEY
uv sync --group dev

uv run --group dev pytest tests/ -v    # 测试名即设计决策清单
uv run python smoke.py                 # provider 层端到端验证

# 批量评测
uv run python -m eval.runner \
    --dataset eval/datasets/bird/dev_20240627 \
    --limit 250 --workers 2 --label baseline
```

数据集下载（330 MB）：

```bash
curl -L -o dev.zip https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip
# 解压后还有一层 dev_databases.zip 需要再解
```

## 实验记录

模型：`cn:deepseek-v4.1-flash`，BIRD dev 随机抽样 250 题（seed=0，11 个库全覆盖）。

| 配置 | 执行准确率 | 有效执行率 | 单题成本 | 单题耗时 |
|---|---|---|---|---|
| baseline（整库 schema，单次生成，无重试） | **50.2%** (125/249) | 98.8% | 0.061 credit | 6.3s |

按难度：simple 53.8% (78/145) · moderate 45.0% (36/80) · challenging 45.8% (11/24)

按数据库，最高到最低：superhero 75.0% · student_club 71.4% · financial 63.2% ·
toxicology 60.0% · european_football_2 50.0% · codebase_community 42.3% ·
thrombosis_prediction 39.4% · card_games 35.5% · formula_1 30.8%

**有效执行率 98.8% 而准确率只有 50.2%**，两者差了 48 个点。这个差值是最重要的信号：
模型的 SQL 语法几乎不出错，错的全是对表结构和业务口径的理解。
所以下一步该补的是知识，不是让模型"更会写 SQL"。

失败归因：结果内容不对 48% · 行数不对 36% · 列数不对 14% · SQL 跑不通 2%。

另有两个值得记的数字：250 题里模型调用失败 0 次、没吐出 SQL 0 次——
退避重试和输出预算两处修复在全量上是稳的。思考 token 占输出的 **94.3%**，
这是后面成本优化最大的一块可压缩空间。

每次评测的逐题明细写在 `eval/results/<label>-<时间戳>.jsonl`，首行是汇总。

---

## 设计决策

### 为什么不用 LangChain

框架把重试策略和 context 裁剪藏在内部。这个项目的核心产出恰恰是
"在什么条件下重试、裁掉什么"的量化结论，藏起来就没得测了。
agent 主循环三百行以内，自己写。

### provider 抽象层

`llm/base.py` 定义中立类型，两个适配器各自序列化。抹平的差异：

| | Anthropic | OpenAI 兼容 |
|---|---|---|
| system prompt | 顶层参数 | `messages[0]` |
| 工具定义 | `input_schema` | `function.parameters` |
| 工具调用返回 | `content` 里的 `tool_use` 块 | 独立的 `tool_calls` 字段 |
| 工具结果回灌 | 一条 user 消息装多个 `tool_result` 块 | 每个结果一条 `role="tool"` 消息 |
| 缓存 token 口径 | **不计入** `input_tokens` | 计入 |
| 缓存开关 | 显式打 `cache_control` | 自动 |

最后两行最容易出错：口径不统一时跨模型成本对比全是错的。

### 成本统计

- 接口上报了实际扣费就以它为准，本地价格表只是兜底
- `credit` 和 `usd` 两种单位**相加直接抛异常**，不静默合并
- 思考 token 单独成列——推理模型把思考按输出计费，不单列的话削减思考预算的优化在账面上看不出来

### 安全

两道独立防线：

1. `sandbox/guard.py` 用 sqlglot 解析 AST，白名单只放行 SELECT/WITH，拦截多语句拼接、DDL/DML、PRAGMA/ATTACH、`readfile` 等文件函数
2. 执行器用 SQLite 的 `mode=ro` URI，即使第一道有洞，驱动层也写不进去

外加超时中断（另起线程调 `interrupt()`，SQLite 的 `timeout` 参数只管锁等待）和行数上限。

### 评测口径

和 BIRD 官方基本一致，一处更严：

- 行顺序默认不敏感，题干出现 `ordered by` / `top N` / `highest` 等表达时才按顺序比
- **行重复敏感**：官方用 `set()`，会把 `[(1,),(1,)]` 和 `[(1,)]` 判成相等，放过 GROUP BY 写错的典型症状
- 列顺序敏感，浮点按 4 位精度比，`1` 和 `1.0` 视为相同
- 标准 SQL 自己跑不通的题从分母里剔除并单独计数

---

## 踩坑记录

按发现顺序，每条都是实测出来的。

### 1. 结果集比对里的三个 bug，全由测试抓出

- **截断检测永远为假**：注入的 `LIMIT` 恰好等于行数上限，取满时无法区分"刚好这么多"和"被截断"。改成注入 `max_rows + 1`，多出来的那行就是截断的证据。
- **`orders` 里含子串 `order`**：`needs_order()` 用子串匹配，把 `"How many orders are there?"` 判成排序题。BIRD 里 `orders` 表随处可见，这个 bug 会让一大批本该答对的题按顺序比对而误判。改成词边界正则。
- **布尔的比对语义**：SQLite 没有布尔类型，用 0/1 存，所以 `True` 和 `1` 本来就该判相等。原先的测试断言反了。

教训：**先用自造的微型数据库把比对逻辑验证正确，再动真实数据集。** 否则等 BIRD 跑完才发现指标算错，钱和时间全白花。

### 2. 推理模型把输出预算烧光，一个字没吐

`max_tokens=2048` 时出现 `reasoning_tokens == 2048 == max_tokens` 的空回复。
思考过程计入输出预算，预算不够就只剩思考没有答案。

这不是模型不会，是配置错了。提到 8192 后消失。

网关支持 `reasoning_effort: low|minimal` 和 `extra_body: {thinking: {type: disabled}}`，
关掉思考能明显省钱——但那属于后面的成本优化实验，不能混进 baseline。

### 3. 并发 4 打爆网关账号池

`503 no_healthy_account`，占错误的 64%。加指数退避重试（带抖动，避免多个 worker
被拒后同时重试）并把默认并发降到 2。

**这两个配置问题合计让准确率从 16.7% 虚低到真实的 60%。**
如果没做失败归因表，会误以为是模型能力不行，转而去调 prompt——方向完全错。

---

## 下一步

baseline 的失败归因指向三个方向，按预期收益排序：

1. **列描述注入** —— BIRD 自带 `database_description/*.csv`，含每列的业务含义和取值说明。
   实测多道错题的根因是模型不知道 `results` 和 `driverStandings` 的区别、
   不知道 `bond_type` 里存的是 `'#'`。这是最大的杠杆。
2. **样例行注入** —— 列名骗不了人但列值会。模型看不到 `type` 列里只有 `'commander'`
   没有 `'expansion'`，就只能猜。
3. **执行反馈重试** —— 报错原文回灌让模型自己改。

每一项都单独跑一次评测，留下消融曲线。完整计划和完成标准见 [docs/ROADMAP.md](docs/ROADMAP.md)。
