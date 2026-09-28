# 沙箱代码待议问题

> 阅读 `sandbox/` 时发现、但**尚未定性**的可疑点。每条记录：位置、观察到的现象、
> 复现方式、可能的影响、以及"我拿不准的地方"。
>
> 处理流程：**先记录，不动代码**。等用户评估后，要修的写进 `DECISIONS.md`，
> 不修的在本文件标注"已确认非问题"并说明原因——留痕比删掉更有价值，
> 免得下一个人重新怀疑一遍。

---

## S1 · `guard.py` 的 `exp.With` 分支恒不成立 ✅ 已修复（2026-09-28，用户敲定）

**位置**：`sandbox/guard.py:97-99`

```python
body = root
if isinstance(root, exp.With):     # ← 这个条件恒为 False
    body = root.this
```

**观察到的现象**：注释写的是"WITH ... SELECT 的顶层节点是被查询包裹的，取出真正的
主体判断"，但 sqlglot 的实际结构和这个假设**正好相反**：

```
作者假设的树：  With( this = Select(...) )       With 在外，Select 在内
实际的树：      Select( with_ = With(...) )     Select 在外，With 是子节点
```

**复现**：

```bash
uv run --group dev python -c "
import sqlglot
from sqlglot import exp
for d in ['sqlite','postgres','mysql','duckdb','bigquery','tsql','snowflake','hive','spark']:
    r = sqlglot.parse('WITH x AS (SELECT 1 AS c) SELECT c FROM x', dialect=d)[0]
    print(d, type(r).__name__, isinstance(r, exp.With))
"
```

9 个方言全部输出 `Select False`。`isinstance(root, exp.With)` **没有一次成立**。

**影响**：目前**不影响正确性**。`body` 保持初值 `root`（一个 `Select`），
而 `Select` 本来就在白名单 `_ALLOWED_ROOT` 里，CTE 查询照常放行。

**反倒是"假如它生效"会出问题**：`Select` 对象的 `.this` 是 `None`，不是子查询。
真让第 99 行执行，`body` 会变成 `None`，第 101 行 `isinstance(None, _ALLOWED_ROOT)`
判 False，报错文案变成"只允许 SELECT 查询，检测到 NONETYPE"——正常查询被误拒，
且理由无法理解。所以这行不是保障，是**埋着的雷**。

**我拿不准的地方**：不确定是不是所有 sqlglot 版本、所有方言都这样（我验的是
30.18.0）。若上游哪天改成 `With` 作根节点，这行会从"死代码"变成"炸点"。

**建议改法**（2026-09-28 提出，待用户敲定）：

1. 删掉 `check()` 里的 97-99 行和 `ensure_limit()` 里 133 行的**同一个死分支**
   （`ensure_limit` 也有 `isinstance(tree, exp.With)`，同样恒不成立），替换为
   说明真实树形的注释。`body` 本来就恒等于 `root`，纯删死代码，行为零变化。
2. 顺手加两个测试钉住树形假设（多 CTE、`WITH ... UNION`），这样上游 sqlglot
   若真改了树形，测试先红，不会静默漏过。

不建议写"兼容未来 With 根节点"的防御代码：如果上游真改了树形，`isinstance`
失败只会让查询被**拒绝**（fail-closed，方向安全），且有测试兜底，届时再改即可。

**实际修复记录（2026-09-28，用户敲定后执行）**：

- `sandbox/guard.py`：`check()` 的拆壳分支已删，改判 `root` 本身；`ensure_limit()`
  的同一死分支一并删除。两处都留了树形注释，并指回本条目。
- `tests/test_guard_dialects.py`：新增 `test_cte_tree_shape_is_pinned`（钉住
  根节点是 Select/Union、`With` 是子节点）和 `test_cte_queries_pass_guard`
  （单 CTE / 多 CTE / `UNION ALL` 三种形态过闸门且补 LIMIT）。
- 教训一条：第一次改的时候漏了错误文案里的 `body` 变量引用，`NameError`
  炸了 6 个测试——**删除一个变量时，全文件搜一遍它的引用**，报错文案
  这种字符串里的 f-string 引用最容易漏。
- 全量测试 138 passed / 24 skipped（PG 库未启动，按约定跳过），无回归。

---

## S2 · `FOR UPDATE` 是否漏检（已确认非问题，留痕）

**位置**：`sandbox/guard.py:24-28`（`_FORBIDDEN` 含 `exp.Lock`）

**背景**：最初读代码时判断"`FOR UPDATE` 会在解析阶段被丢弃，`exp.Lock` 永远
匹配不到，闸门会放行加锁查询"。**这个判断是错的**，核对后更正如下。

**实际行为**：sqlglot 会把 `FOR UPDATE` 解析成 `Lock` 节点**保留在树里**，
只是在**重新渲染 SQL 文本**时才丢掉它：

```
解析出的树里有 Lock 节点 : ['Lock']           ← walk() 能扫到，防线有效
重新渲染回文本           : SELECT * FROM t     ← 只有这一步 FOR UPDATE 消失
渲染成 postgres 方言     : SELECT * FROM t FOR UPDATE
```

`guard.py:106` 的 `root.walk()` 遍历的是**树**，不是渲染后的文本，所以
`exp.Lock` 能被正常检出。

**验证**：

```bash
uv run --group dev python -c "
from sandbox.guard import check
for d in ['sqlite','postgres','mysql']:
    r = check('SELECT * FROM t FOR UPDATE', dialect=d)
    print(d, r.ok, r.reason)
"
```

三个方言全部 `ok=False`（`禁止的操作：LOCK`）；`FOR SHARE` 和
`FOR UPDATE NOWAIT` 同样被拒。

**结论**：防线没有漏洞，无需改动。记录在此是因为**当初差点据此提一个错误的
修复**——`walk()` 扫树、`.sql()` 渲染文本，这两件事结果不同，容易看走眼。

---

## S3 · `SELECT ... INTO` 的覆盖情况（已验证，结论有保留）

**位置**：`sandbox/guard.py:24-28`（`_FORBIDDEN` 含 `exp.Into`）

**观察到的现象**：注释说 `Into` 用于挡 PG 的 `SELECT INTO`（等于建表）和 MySQL 的
`INTO OUTFILE`（写服务器文件）。实测这两条路径**靠的是不同机制**：

```
postgres  SELECT * INTO new_t FROM t           -> 树里有 Into 节点 -> 按 exp.Into 挡住 ✓
tsql      SELECT * INTO new_t FROM t           -> 树里有 Into 节点 -> 按 exp.Into 挡住 ✓
mysql     SELECT * FROM t INTO OUTFILE '/tmp/x'  -> 解析直接抛 ParseError -> 按"无法解析"挡住 ✓
mysql     SELECT * FROM t INTO DUMPFILE '/tmp/x' -> 解析直接抛 ParseError -> 按"无法解析"挡住 ✓
```

三类写法**都被拒绝了**，当前没有漏洞。

**保留意见**：MySQL 的 `INTO OUTFILE` / `INTO DUMPFILE` 是**合法 MySQL 语法**，
sqlglot 30.18.0 根本不支持它——拒绝是**撞上了解析失败的兜底**，不是 `exp.Into`
起了作用。两处隐患：

1. 拒绝理由是"SQL 无法解析：Invalid expression…"，回灌给模型的话术不准确，
   模型看不懂为什么要改。
2. 若上游 sqlglot 未来支持了 `INTO OUTFILE` 并把它建模成 `Into` 以外的节点
   （例如 `exp.Anonymous` 或新的表达式类），这道防线就会**从兜底变成真空**。

**建议**（待用户定夺）：为 MySQL 方言补一条针对 `OUTFILE` / `DUMPFILE` 关键字的
显式检查，不依赖解析器恰好失败。优先级低——MySQL 执行器 (`MySQLSandbox`)
目前还没实现（见 `DESIGN.md` 3.2）。

---

## 待议汇总

| # | 事项 | 严重度 | 状态 |
|---|---|---|---|
| S1 | `guard.py:97-99` 的 `exp.With` 分支恒不成立 | 低（现状无害，上游若变则炸） | ✅ 09-28 已修复，见 S1 末尾的实际修复记录 |
| S2 | `FOR UPDATE` 是否漏检 | — | ✅ 已确认非问题 |
| S3 | MySQL `INTO OUTFILE` 靠解析失败兜底，非显式拦截 | 低（MySQL 执行器未实现） | 已验证，待用户定夺 |

---

## 为什么记在 docs/ 而不是 issue

这些疑点都需要**读过 sandbox 源码 + 跑过 sqlglot 实验**才能提出来，
写成本文件后，下次有人（人或 AI）读 `guard.py` 时不必重跑同样的实验。
处理完的条目不要删——在状态列标注结论即可，`S2` 就是例子：
它现在的价值不是"待修"，而是记录"`walk()` 扫树、`.sql()` 渲染文本，
两者结果不同"这个容易看走眼的点。
