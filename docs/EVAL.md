# 评测协议

> 所有实验数字都必须按本协议产出，否则不得写进 README、简历或拿去和别的数字比较。
> 协议本身有改动时，在文末「口径变更记录」登记，并说明哪些旧数字因此作废。

---

## 1. 数据集

| 项 | 值 |
|---|---|
| 数据集 | BIRD dev（`dev_20240627` 版本） |
| 来源 | `https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip` |
| 本地路径 | `eval/datasets/bird/dev_20240627/`（不进 git） |
| 规模 | 1534 题，11 个库；simple 925 / moderate 464 / challenging 145 |
| 带 evidence | 1386 / 1534 |

`eval/datasets/mini/` 是自造的 6 题微型集，**只用于验证管线本身能跑、比对逻辑正确**，
它的准确率没有任何意义，不得引用。

### 1.1 多方言评测：BIRD Mini-Dev

| 项 | 值 |
|---|---|
| 数据集 | BIRD Mini-Dev，官方同一批题提供 SQLite / MySQL / PostgreSQL 三版标准 SQL |
| 来源 | `https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip`（800 MB） |
| 本地路径 | `eval/datasets/bird_minidev/minidev/`（不进 git） |
| 规模 | 500 题，11 个库；simple 148 / moderate 250 / challenging 102 |
| PG 评测库 | `eval/pg/docker-compose.yml`：官方导出按 `db_id` 拆进 11 个 schema，每道题用 `search_path` 只看得到自己的库 |

- **名字别混**：Mini-Dev 是 BIRD 官方的 500 题子集，按本协议跑出的数字可以引用；
  `eval/datasets/mini/` 是自造的 6 题管线自检集，数字不得引用。
- **不能和 dev 的数字比**：Mini-Dev 里 moderate + challenging 占 70%（dev 随机抽样约 40%），
  它的准确率和 baseline 的 50.2% 不可比。跨方言比较必须在 Mini-Dev 同一批题上各跑一次。
- **只换方言一个变量**：system prompt 只把"SQLite"换成目标方言名，其余一字不改
  （`agent/baseline_dialect.py`）；schema 同样是整库 DDL，不带样例行。
- **成本**：Mini-Dev 比 dev 难，单题成本约为 dev 的 2 倍，250 题约 33 credit、并发 2 约 13 分钟。
- **同一批题**：标准配置同第 2 节（`--limit 250 --seed 0`）。SQLite 版和 PG 版题目文件的题号顺序一致，
  同一 seed 抽到的是同一批题，可以逐题配对比较。

## 2. 标准实验配置

除非实验本身就是在研究某个参数，否则以下参数固定：

| 参数 | 值 | 说明 |
|---|---|---|
| `--limit` | 250 | 从 1534 题中抽样 |
| `--seed` | 0 | 固定种子随机抽样，覆盖全部 11 个库 |
| `--workers` | 2 | 并发 4 会触发网关 503 |
| 模型 | `cn:deepseek-v4.1-flash` | 所有角色 |
| `sql_gen.max_tokens` | 8192 | 推理模型的思考计入输出预算 |
| `temperature` | 0 | |
| `--max-rows` | 2000 | |

抽样不取前 N 条：BIRD 的题按库聚集，取前 N 条等于只测两三个库。

**30 题的探路跑只用于发现系统性问题**，置信区间约 ±9 个点，数字不得引用。
实测：同样配置，30 题 60.0%，250 题 50.2%。

## 3. 比对口径

实现在 `eval/metrics.py`，每条规则都有对应测试。

| 规则 | 本项目 | BIRD 官方 |
|---|---|---|
| 行顺序 | 默认不敏感；题干要求排序时敏感 | 不敏感 |
| 行重复 | **敏感**（多重集比较） | 不敏感（`set()`） |
| 列顺序 | 敏感 | 敏感 |
| 浮点 | 保留 4 位小数后比较 | 精确比较 |
| `1` vs `1.0` | 相等 | 视实现而定 |
| `True` vs `1` | 相等（SQLite 用 0/1 存布尔） | — |
| 字符串首尾空白 | 忽略 | 不忽略 |
| NULL | 只和 NULL 相等 | 同 |

**和官方口径不同，本项目数字不能直接和 BIRD 排行榜比较。** 对外引用时必须注明
"本项目口径，行重复敏感"。

### 排序判定

`needs_order(question)` 是按词边界的启发式正则，命中 `ordered by` `top N` `highest`
`descending` 等表达时按顺序比对。原则是**宁可漏判，不可误判**——误判会把正确答案判错。

必须用词边界：BIRD 里 `orders` 表随处可见，子串匹配会把
"How many orders are there?" 判成排序题。

### 作废题

标准 SQL 自己执行失败的题从分母中剔除，单独计入 `gold_failed`。
baseline 250 题中有 1 题作废。

## 4. 指标

| 指标 | 定义 | 用途 |
|---|---|---|
| 执行准确率 | 结果集一致题数 / 有效题数 | 主指标 |
| 有效执行率 | 预测 SQL 能执行成功的题数 / 有效题数 | 和准确率的差值指出问题在语法还是理解 |
| 单题成本 | 总 credit / 有效题数 | 含全部重试 |
| 单题 token | 输入、输出分列；缓存命中、思考 token 单列 | 成本归因 |
| 单题耗时 | 从读 schema 到比对完成 | |

任何"准确率提升"的结论都必须同时报告成本变化。

## 5. 失败归因分类

`runner.print_report` 按以下顺序归类，每道错题只归一类：

| 类别 | 判据 | 通常意味着 |
|---|---|---|
| 没吐出 SQL | 回复中提取不到 SQL | 输出预算不够、格式约束失效 |
| SQL 跑不通 | 预测 SQL 执行报错 | 列名/表名幻觉、方言问题 |
| 模型调用失败 | 重试后仍失败 | 网关问题，**不是模型能力问题** |
| 结果行数不对 | 行数不一致 | 过滤条件、JOIN、GROUP BY 错误 |
| 结果列数不对 | 列数不一致 | 多选或少选了输出列 |
| 结果内容不对 | 其余 | 口径理解、选错表、计算方式 |

"模型调用失败"和"没吐出 SQL"在正常配置下应该接近 0。如果不是，**先修配置再看准确率**——
项目初期这两类曾让准确率从真实的 60% 虚低到 16.7%。

## 6. 结果文件

路径：`eval/results/<label>-<YYYYMMDD-HHMMSS>.jsonl`

- 第 1 行：本次运行的汇总与参数（label、model、n、limit、seed、sample_rows、dialect、questions、accuracy、exec_rate、cost、cost_unit、gold_failed、wall_s、completed、aborted）
- **熔断**：连续 3 题模型调用失败（通常是限流）时 runner 停止派发新题，已完成的照常写盘，`aborted` 记录原因。`aborted` 非空的结果文件**数字不得引用**，只作为逐题明细保留
- 第 2 行起：每题一条 `Record`，字段见 `eval/runner.py`

结果文件**进 git**，是所有结论的原始证据。不要删除旧结果，即使它来自有 bug 的版本——
修复前后的对照本身有价值（例如 `probe` 与 `probe2`）。

⚠️ 当前汇总行缺少「口径版本」和「代码版本（git commit）」，见 ROADMAP 待决事项。

## 7. 新增一个实验的流程

1. 在 ROADMAP 里确认这个实验要回答什么问题
2. **先问用户**：250 题一次约 15 credit、15 分钟
3. 用唯一且可读的 `--label` 跑，其余参数保持标准配置
4. 把结果登记进下面的实验登记表，写一句结论
5. 结论和预期不符时，先看失败归因和逐题明细，排除配置问题，再下结论

## 8. 实验登记表

| label | 日期 | 改动 | 题数 | 准确率 | 有效执行率 | 单题成本 | 结论 |
|---|---|---|---|---|---|---|---|
| `mini-baseline` | 09-20 | 管线自检 | 6 | 100% | 100% | 0.008 | 管线通，数字无意义 |
| `probe` | 09-20 | 首次探路 | 30 | 16.7% | 36.7% | 0.025 | 16 题网关 503，3 题思考烧光预算，**作废** |
| `probe2` | 09-20 | 修复重试与预算 | 30 | 60.0% | 96.7% | 0.077 | 配置问题消除；样本太小不引用 |
| **`baseline`** | 09-20 | 整库 schema、单次生成 | 249 | **50.2%** | 98.8% | 0.061 | 正式对照组 |
| `mini-check-0925` | 09-25 | runner 支持 PG 后的 SQLite 管线自检 | 6 | 100% | 100% | 0.018 | 老路径正常，数字无意义 |
| `minidev-pg-check` | 09-25 | Mini-Dev PG 管线自检 | 6 | 83.3% | 100% | 0.170 | PG 路径走通，数字无意义 |
| `mini-check-0925b` | 09-25 | runner 加熔断后的端到端自检 | 6 | 100% | 100% | 0.015 | 写盘和汇总字段正常，数字无意义 |
| **`minidev-pg-baseline`** | 09-25 | Mini-Dev **PostgreSQL** 版，整库 schema、单次生成，prompt 只换方言名 | 250 | **47.6%** | 96.8% | 0.131 | PG 首个正式数字；**不能和 50.2% 比**，要和 Mini-Dev SQLite 同一批题对照 |

### baseline 明细

- 按难度：simple 53.8% (78/145) · moderate 45.0% (36/80) · challenging 45.8% (11/24)
- 按库：superhero 75.0% · student_club 71.4% · financial 63.2% · toxicology 60.0% ·
  european_football_2 50.0% · codebase_community 42.3% · thrombosis_prediction 39.4% ·
  card_games 35.5% · formula_1 30.8%
- 失败归因：内容不对 48% · 行数不对 36% · 列数不对 14% · 跑不通 2%
- 调用失败 0，没吐出 SQL 0
- 思考 token 占输出 94.3%，缓存命中 67.1%
- 总成本 15.2 credit，墙钟 824s

**解读**：有效执行率和准确率相差 48 个点，错误集中在理解而非语法。
下一步应补充知识（列描述、样例值），而不是调整 prompt 让模型"更会写 SQL"。

### minidev-pg-baseline 明细

- 按难度：simple 61.8% (42/68) · moderate 48.0% (59/123) · challenging 30.5% (18/59)
- 按库：superhero 72.0% · financial 64.3% · codebase_community 62.1% · student_club 61.5% ·
  formula_1 44.8% · european_football_2 43.5% · thrombosis_prediction 39.1% · toxicology 34.8% ·
  debit_card_specializing 33.3% · card_games 32.1% · california_schools 27.8%
- 失败归因：行数不对 39% · 内容不对 37% · 列数不对 18% · 跑不通 5% · 没吐出 SQL 1%
- **方言残留**：跑不通的 7 题里 4 题是 SQLite 写法——3 题用 `strftime`，1 题对 `date` 列用 `LIKE`。
  prompt 明确写了 PostgreSQL 仍会发生
- 没吐出 SQL 1 题：思考用满 8192 token（同 D9）
- 调用失败 0，熔断未触发；作废题 0
- 标准答案超过 2000 行的 7 题：4 对 3 错；其中 2 题预测行数远少于 2000，确实错；1 题（346）两边都被截断，
  无法确认，对准确率的影响不超过 0.4 个点
- 思考 token 占输出 93.5%，缓存命中 61.9%；单题成本约为 dev baseline 的 2 倍（题更难，思考更多）
- 总成本 32.8 credit，墙钟 749s（并发 2）

**解读**：有效执行率和准确率相差 49 个点，和 SQLite 上一样，错误集中在理解而非语法。
方言直接造成的失败只有约 4 题（1.6 个点）。PG 相对 SQLite 到底差多少，要在 Mini-Dev SQLite 版
同一批 250 题上跑对照才能回答（预计约 30 credit）。

## 9. 口径变更记录

| 日期 | 变更 | 影响 |
|---|---|---|
| 09-20 | 初版 | — |
| 09-25 | 新增 Mini-Dev 多方言评测（1.1 节）；runner 支持 PG | 不影响已有数字：SQLite 路径逐条验证一致，见 D14、D15 |
