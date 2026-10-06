# 2.4 执行反馈循环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把评测链路的单次生成换成工具循环——模型可以用 `execute_sql` 试跑、看真实结果、自己修正，用 `submit_sql` 交卷。

**Architecture:** 新增 `agent/events.py`（事件类型）、`agent/tools.py`（工具定义）、`agent/core.py`（循环 + 事件流）。循环只产出事件，runner 用 `consume()` 收敛成结果。工具执行的 SQL 走**同一个 `sandbox.run()`**，不新增攻击面。

**Tech Stack:** Python 3.11、现有 `llm/`（ToolSpec/ToolCall/ToolResult/Usage）、现有 `sandbox/`（Sandbox 协议）、pytest。

**Spec:** `docs/superpowers/specs/2026-10-06-agent-loop-design.md`

## Global Constraints

- 注释、docstring、报错文案用中文；标识符用英文
- `llm/` 之外的代码**禁止** `import openai` / `import anthropic`
- 失败返回结果对象，**不抛异常到评测主循环**（单题炸掉不能中断整轮）
- 不写死模型名，模型只通过 `Router.for_role()` 获取
- 测试命令：`PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/ -q`
- **提交前必须先问用户**（CLAUDE.md 规定 git 提交属于"必须先问用户的事"）
- `--tools` 默认关：关闭时请求必须与现有单次路径**逐字节一致**，已有数字不受影响
- `max_steps` 是防跑飞的兜底，不是预算；撞到上限记 `hit_cap=True`，属异常信号

---

### Task 1: 事件类型 `agent/events.py`

**Files:**
- Create: `agent/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: `llm.base.Usage`
- Produces: `AgentEvent(type, payload, usage, ts)`；`EventType` 字面量

- [ ] **Step 1: 写失败测试**

```python
"""事件类型：主循环产出、CLI/Web/runner 消费的公共契约。"""

from __future__ import annotations

from agent.events import AgentEvent
from llm.base import Usage


def test_defaults_are_empty():
    e = AgentEvent("final")
    assert e.type == "final"
    assert e.payload == {}
    assert e.usage is None
    assert e.ts > 0


def test_carries_payload_and_usage():
    u = Usage(input_tokens=10, output_tokens=5)
    e = AgentEvent("final", {"sql": "SELECT 1", "steps": 2}, usage=u)
    assert e.payload["sql"] == "SELECT 1"
    assert e.usage is u


def test_ts_is_independent_per_event():
    assert AgentEvent("step_start").ts <= AgentEvent("step_start").ts
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/test_events.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.events'`

- [ ] **Step 3: 实现**

```python
"""agent 主循环产出的事件流。

主循环只产出事件，不做任何渲染。CLI、Web、轨迹存储、评测 runner 都是消费者——
一套引擎可以同时被人看、被存、被回放、被评测。见 docs/DESIGN.md 4.4。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from llm.base import Usage

EventType = Literal[
    "step_start", "tool_call", "tool_result", "final", "error",
    # 阶段 3 预留：planner / verifier / 重试
    "plan", "verify", "retry",
]


@dataclass(slots=True)
class AgentEvent:
    """一次循环产出的单个事件。``payload`` 放类型相关的数据，``usage`` 只在该事件
    消耗了模型调用时带上。"""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    usage: Usage | None = None
    ts: float = field(default_factory=time.time)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/test_events.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**（先问用户）

```bash
git add agent/events.py tests/test_events.py
git commit -m "2.4 事件类型：AgentEvent 契约"
```

---

### Task 2: 工具定义 `agent/tools.py`

**Files:**
- Create: `agent/tools.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `llm.base.ToolSpec`
- Produces: `EXECUTE_SQL`、`SUBMIT_SQL`（均为 `ToolSpec`）、`TOOLS: list[ToolSpec]`

- [ ] **Step 1: 写失败测试**

```python
"""工具定义：只给两个，别的一律不加。"""

from __future__ import annotations

from agent.tools import EXECUTE_SQL, SUBMIT_SQL, TOOLS


def test_exactly_two_tools():
    assert [t.name for t in TOOLS] == ["execute_sql", "submit_sql"]


def test_both_require_sql_arg():
    for t in (EXECUTE_SQL, SUBMIT_SQL):
        assert t.parameters["required"] == ["sql"]
        assert "sql" in t.parameters["properties"]


def test_descriptions_are_nonempty():
    for t in TOOLS:
        assert t.description.strip()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/test_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.tools'`

- [ ] **Step 3: 实现**

```python
"""暴露给模型的工具：试跑 SQL、交卷。

只给两个。schema 整份已经在 prompt 里，取数用 `SELECT ... LIMIT` 自己就能办到——
工具由错误数据逼出来，不拍脑袋加。见设计文档第 7 节。
"""

from __future__ import annotations

from llm.base import ToolSpec

EXECUTE_SQL = ToolSpec(
    name="execute_sql",
    description="在数据库上执行一条只读 SELECT 查询，返回结果行。用于验证查询是否正确。",
    parameters={
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "要执行的 SELECT 语句"}},
        "required": ["sql"],
    },
)

SUBMIT_SQL = ToolSpec(
    name="submit_sql",
    description="提交你的最终 SQL 答案，结束本轮任务。",
    parameters={
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "最终的 SELECT 语句"}},
        "required": ["sql"],
    },
)

TOOLS: list[ToolSpec] = [EXECUTE_SQL, SUBMIT_SQL]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/test_tools.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**（先问用户）

```bash
git add agent/tools.py tests/test_tools.py
git commit -m "2.4 工具定义：execute_sql + submit_sql"
```

---

### Task 3: 循环主路径 `agent/core.py`

**Files:**
- Create: `agent/core.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `agent.baseline`（`SYSTEM` / `USER_TEMPLATE` / `extract_sql`）、`agent.events.AgentEvent`、`agent.tools.TOOLS`、`llm.base`（`LLMProvider` / `Message` / `ToolResult` / `Usage`）、`sandbox.base.Sandbox`
- Produces:
  - `run(question, *, provider, sandbox, schema, evidence="", max_steps=10, max_tokens=8192) -> Iterator[AgentEvent]`
  - `consume(events: Iterator[AgentEvent]) -> AgentOutcome`
  - `AgentOutcome(sql, steps, tool_calls, hit_cap, usage, error)`
  - `SYSTEM`（baseline.SYSTEM + 工具说明）

- [ ] **Step 1: 写失败测试**

```python
"""主循环：模型调 execute_sql 试跑、调 submit_sql 交卷。全程用假 provider / 假 sandbox。"""

from __future__ import annotations

from agent import core
from agent.events import AgentEvent
from llm.base import LLMResponse, ToolCall, Usage
from sandbox.base import ExecResult


class FakeProvider:
    """按脚本依次返回响应；每次调用记下参数。"""

    name = "fake"
    model = "fake"

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return self.script.pop(0)


class FakeSandbox:
    dialect = "sqlite"

    def __init__(self, results: dict[str, ExecResult] | None = None) -> None:
        self.results = results or {}
        self.ran: list[str] = []

    def run(self, sql: str, *, enforce_limit: bool = True) -> ExecResult:
        self.ran.append(sql)
        return self.results.get(sql, ExecResult(ok=True, columns=["a"], rows=[(1,)]))


def _resp(text=None, calls=None):
    return LLMResponse(text=text, tool_calls=calls or [], stop_reason="end",
                       usage=Usage(input_tokens=10, output_tokens=5))


def _run(script, sandbox=None, **kw):
    p = FakeProvider(script)
    events = list(core.run("问题", provider=p, sandbox=sandbox or FakeSandbox(),
                           schema="CREATE TABLE t (a INT);", **kw))
    return p, events, core.consume(iter(events))


def test_execute_then_submit():
    """先试跑一条，看到结果后用 submit_sql 交卷。"""
    sandbox = FakeSandbox()
    script = [
        _resp(calls=[ToolCall("c1", "execute_sql", {"sql": "SELECT a FROM t"})]),
        _resp(calls=[ToolCall("c2", "submit_sql", {"sql": "SELECT a FROM t WHERE a > 1"})]),
    ]
    p, events, out = _run(script, sandbox)
    assert sandbox.ran == ["SELECT a FROM t"]
    assert out.sql == "SELECT a FROM t WHERE a > 1"
    assert out.steps == 2
    assert out.tool_calls == 2
    assert out.hit_cap is False
    assert [e.type for e in events] == [
        "step_start", "tool_call", "tool_result", "step_start", "final",
    ]


def test_submit_directly_without_trying():
    """一步收工：不试跑，直接交卷。"""
    _, _, out = _run([_resp(calls=[ToolCall("c1", "submit_sql", {"sql": "SELECT 1"})])])
    assert out.sql == "SELECT 1" and out.steps == 1 and out.tool_calls == 1


def test_tool_result_carries_real_rows():
    """execute_sql 的结果必须回灌给模型，否则它看不到自己写对没有。"""
    sandbox = FakeSandbox({"SELECT a FROM t": ExecResult(ok=True, columns=["a"], rows=[(1,), (2,)])})
    script = [
        _resp(calls=[ToolCall("c1", "execute_sql", {"sql": "SELECT a FROM t"})]),
        _resp(calls=[ToolCall("c2", "submit_sql", {"sql": "SELECT a FROM t"})]),
    ]
    p, _, _ = _run(script, sandbox)
    # 第二次调用的 messages 里必须有一条携带工具结果的 user 消息
    second = p.calls[1]["messages"]
    results = [m for m in second if m.tool_results]
    assert results and results[0].tool_results[0].content.startswith("a\n1")
    assert results[0].tool_results[0].is_error is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/test_core.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.core'`

- [ ] **Step 3: 实现**

```python
"""agent 主循环：给模型工具，让它试跑 SQL、自己决定何时交卷。

循环只产出事件流，不做渲染。runner 用 ``consume()`` 收敛成结果对象。
2.4 的靶子很小（SQL 跑不通 + 没吐出 SQL 约 5 题），价值在架构：planner / verifier /
CLI / Web 将来都挂在这套事件流上。见 docs/DESIGN.md 4.4。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from agent import baseline
from agent.events import AgentEvent
from agent.tools import TOOLS
from llm.base import LLMError, LLMProvider, Message, ToolResult, Usage
from sandbox.base import Sandbox

# 防跑飞的兜底，不是预算：正常模型 1-3 步交卷，撞到它说明模型绕不出来。
MAX_STEPS_DEFAULT = 10

SYSTEM = baseline.SYSTEM + """

你可以调用以下工具：
- execute_sql：试跑一条查询，看真实结果，用来验证你的 SQL 是否正确。
- submit_sql：确定之后，用它提交最终答案。
不确定时先 execute_sql 验证，确认无误再用 submit_sql 交卷。"""


@dataclass(slots=True)
class AgentOutcome:
    """一次 agent 运行的结果，供 runner 使用。"""

    sql: str = ""
    steps: int = 0
    tool_calls: int = 0
    hit_cap: bool = False
    usage: Usage = field(default_factory=Usage)
    error: str = ""


def run(
    question: str,
    *,
    provider: LLMProvider,
    sandbox: Sandbox,
    schema: str,
    evidence: str = "",
    max_steps: int = MAX_STEPS_DEFAULT,
    max_tokens: int = 8192,
) -> Iterator[AgentEvent]:
    """跑一轮工具循环，产出事件流。最后必是一个 ``final`` 或 ``error``。"""
    ev = f"\n业务口径说明：{evidence}\n" if evidence else ""
    messages = [Message.user(baseline.USER_TEMPLATE.format(
        schema=schema, evidence=ev, question=question))]
    usage = Usage()
    n_calls = 0

    for step in range(1, max_steps + 1):
        yield AgentEvent("step_start", {"step": step})
        try:
            resp = provider.chat(
                system=SYSTEM, messages=messages, tools=TOOLS, max_tokens=max_tokens,
            )
        except LLMError as exc:
            yield AgentEvent("error", {"message": str(exc)},
                             usage=usage)
            return
        usage = usage + resp.usage

        if not resp.tool_calls:
            # 模型没调工具就停了：走兜底，从文字里抠（和单次路径同源）。
            yield AgentEvent("final", {
                "sql": baseline.extract_sql(resp.text),
                "steps": step, "tool_calls": n_calls, "hit_cap": False,
            }, usage=usage)
            return

        messages.append(resp.to_message())
        results: list[ToolResult] = []
        for tc in resp.tool_calls:
            n_calls += 1
            if tc.name == "submit_sql":
                yield AgentEvent("final", {
                    "sql": str(tc.args.get("sql", "")),
                    "steps": step, "tool_calls": n_calls, "hit_cap": False,
                }, usage=usage)
                return
            if tc.name != "execute_sql":
                results.append(ToolResult(
                    call_id=tc.id, content=f"未知工具：{tc.name}", is_error=True))
                continue
            res = sandbox.run(str(tc.args.get("sql", "")))
            yield AgentEvent("tool_call", {"name": tc.name, "args": tc.args})
            yield AgentEvent("tool_result", {
                "name": tc.name, "ok": res.ok, "rows": len(res.rows), "error": res.error,
            })
            # to_markdown 只给前 20 行：结果集最多 2000 行，全塞进 context 太贵。
            results.append(ToolResult(
                call_id=tc.id, content=res.to_markdown(), is_error=not res.ok))
        messages.append(Message.results(results))

    yield AgentEvent("final", {
        "sql": "", "steps": max_steps, "tool_calls": n_calls, "hit_cap": True,
    }, usage=usage)


def consume(events: Iterator[AgentEvent]) -> AgentOutcome:
    """把事件流收敛成结果对象。最后一个 ``final`` / ``error`` 决定结果。"""
    out = AgentOutcome()
    for e in events:
        if e.type in ("final", "error"):
            p = e.payload
            out.sql = p.get("sql", "")
            out.error = p.get("message", "")
            out.steps = p.get("steps", 0)
            out.tool_calls = p.get("tool_calls", 0)
            out.hit_cap = p.get("hit_cap", False)
            out.usage = e.usage or Usage()
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/test_core.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**（先问用户）

```bash
git add agent/core.py tests/test_core.py
git commit -m "2.4 主循环：工具调用 + 事件流"
```

---

### Task 4: 循环边界 `agent/core.py`

**Files:**
- Modify: `agent/core.py`（若 Task 3 实现已覆盖，本任务只补测试，不改代码）
- Test: `tests/test_core.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `run` / `consume` / `AgentOutcome`
- Produces: 无新接口

- [ ] **Step 1: 追加失败测试**

在 `tests/test_core.py` 末尾追加：

```python
def test_max_steps_marks_hit_cap():
    """模型一直试跑不收工：撞上限，hit_cap=True，不抛异常。"""
    script = [_resp(calls=[ToolCall(f"c{i}", "execute_sql", {"sql": "SELECT a FROM t"})])
              for i in range(3)]
    _, events, out = _run(script, max_steps=3)
    assert out.hit_cap is True
    assert out.steps == 3
    assert out.tool_calls == 3
    assert events[-1].type == "final"


def test_text_only_falls_back_to_extract():
    """模型没调工具、只回文字：兜底从文字里抠 SQL。"""
    _, _, out = _run([_resp(text="```sql\nSELECT a FROM t\n```")])
    assert out.sql == "SELECT a FROM t"
    assert out.tool_calls == 0 and out.hit_cap is False


def test_failed_execution_feeds_error_back():
    """试跑报错：错误原文回灌，is_error=True。"""
    sandbox = FakeSandbox({"SELECT bad": ExecResult(ok=False, error="no such column: bad")})
    script = [
        _resp(calls=[ToolCall("c1", "execute_sql", {"sql": "SELECT bad"})]),
        _resp(calls=[ToolCall("c2", "submit_sql", {"sql": "SELECT a FROM t"})]),
    ]
    p, _, out = _run(script, sandbox)
    results = [m for m in p.calls[1]["messages"] if m.tool_results]
    assert results[0].tool_results[0].is_error is True
    assert "no such column: bad" in results[0].tool_results[0].content
    assert out.sql == "SELECT a FROM t"


def test_usage_is_summed_across_calls():
    """两次调用的 token 要相加，否则成本算错。"""
    script = [
        _resp(calls=[ToolCall("c1", "execute_sql", {"sql": "SELECT a FROM t"})]),
        _resp(calls=[ToolCall("c2", "submit_sql", {"sql": "SELECT 1"})]),
    ]
    _, _, out = _run(script)
    assert out.usage.input_tokens == 20
    assert out.usage.output_tokens == 10


def test_llm_error_converges_to_error_event():
    """provider 抛 LLMError：收敛成 error 事件，不冒泡。"""
    class BoomProvider:
        name = model = "boom"

        def chat(self, **kwargs):
            raise LLMError("网关 503", provider="fake", retryable=True)

    events = list(core.run("问题", provider=BoomProvider(), sandbox=FakeSandbox(),
                           schema=""))
    assert events[-1].type == "error"
    out = core.consume(iter(events))
    assert "503" in out.error


def test_unknown_tool_is_reported_not_crashed():
    """模型喊了不存在的工具：回一条错误结果，循环继续。"""
    script = [
        _resp(calls=[ToolCall("c1", "no_such_tool", {})]),
        _resp(calls=[ToolCall("c2", "submit_sql", {"sql": "SELECT 1"})]),
    ]
    p, _, out = _run(script)
    results = [m for m in p.calls[1]["messages"] if m.tool_results]
    assert results[0].tool_results[0].is_error is True
    assert out.sql == "SELECT 1"
```

- [ ] **Step 2: 跑测试**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/test_core.py -q`
Expected: 若 Task 3 的实现已覆盖全部行为则 9 passed；否则先 FAIL，按 Step 3 补齐。

- [ ] **Step 3: 补齐实现（仅当有测试失败）**

按失败信息修 `agent/core.py`。已知实现已覆盖：`hit_cap`（循环末尾 yield）、兜底 `extract_sql`、
`is_error=not res.ok`、`usage = usage + resp.usage`、`LLMError` 捕获、未知工具分支。
**不要为了"更完整"加没测到的逻辑。**

- [ ] **Step 4: 跑全量测试**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/ -q`
Expected: 全绿（PG 集成测试未起库时跳过）

- [ ] **Step 5: 提交**（先问用户）

```bash
git add tests/test_core.py agent/core.py
git commit -m "2.4 循环边界：上限、兜底、错误收敛、usage 聚合"
```

---

### Task 5: runner 接线 `eval/runner.py`

**Files:**
- Modify: `eval/runner.py`（import、`Record`、`run_one`、`main`）
- Test: `tests/test_runner_tools.py`

**Interfaces:**
- Consumes: `agent.core.run` / `consume` / `AgentOutcome`
- Produces: `run_one(..., use_tools: bool = False)`；`Record` 新增 `steps` / `tool_calls` / `hit_cap`

- [ ] **Step 1: 写失败测试**

```python
"""runner 接线：--tools 关时必须走老路，逐字节不变。"""

from __future__ import annotations

from agent import baseline, baseline_dialect
from eval.dataset import Item
from eval.runner import run_one
from llm.base import LLMResponse, Usage


class FakeProvider:
    name = model = "fake"

    def __init__(self, reply="```sql\nSELECT 1\n```"):
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(text=self.reply, tool_calls=[], stop_reason="end",
                           usage=Usage(input_tokens=3, output_tokens=2))


class FakeRouter:
    def __init__(self, provider):
        self.provider = provider

    def for_role(self, role):
        return self.provider

    def max_tokens_for(self, role):
        return 8192


def _item(db):
    return Item(qid="t1", db_id="db", question="有多少行？",
                gold_sql="SELECT 1", evidence="", difficulty="simple", db=db)


def test_tools_off_keeps_single_shot(sales_db):
    """开关关着时，发给模型的请求里不能有 tools，system 还是方言版原文。"""
    p = FakeProvider()
    run_one(_item(sales_db), FakeRouter(p), sample_rows=0, max_rows=2000)
    assert "tools" not in p.calls[0] or p.calls[0]["tools"] is None
    assert p.calls[0]["system"] == baseline_dialect.system_prompt("sqlite")


def test_tools_on_uses_the_loop(sales_db):
    """开关打开时，system 变成带工具说明的版本，且请求带上 tools。"""
    from agent.core import SYSTEM

    p = FakeProvider(reply="")   # 无工具调用 → 循环走兜底收工
    run_one(_item(sales_db), FakeRouter(p), sample_rows=0, max_rows=2000, use_tools=True)
    assert p.calls[0]["system"] == SYSTEM
    assert [t.name for t in p.calls[0]["tools"]] == ["execute_sql", "submit_sql"]


def test_record_carries_loop_fields(sales_db):
    p = FakeProvider(reply="")
    rec = run_one(_item(sales_db), FakeRouter(p), sample_rows=0, max_rows=2000, use_tools=True)
    assert rec.steps == 1 and rec.tool_calls == 0 and rec.hit_cap is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/test_runner_tools.py -q`
Expected: FAIL — `TypeError: run_one() got an unexpected keyword argument 'use_tools'`

- [ ] **Step 3: 实现**

3a. 顶部加 import（放在现有 `from agent.baseline_dialect import generate_sql` 之前）：

```python
from agent import core
from agent.baseline_dialect import generate_sql
```

3b. `Record` 末尾加三个字段（带默认值，不影响现有构造）：

```python
    schema_chars: int = 0
    error: str = ""
    steps: int = 0
    tool_calls: int = 0
    hit_cap: bool = False
```

3c. `run_one` 签名加参数：

```python
    column_descriptions: bool = False,
    with_column_values: bool = False,
    use_tools: bool = False,
) -> Record:
```

3d. 把现有生成那一段（`try: gen = generate_sql(...)` 到 `rec = Record(...)`）改成先取
`sql / usage / err / steps / tool_calls / hit_cap`，两条路径汇合后再建 Record：

```python
    provider = router.for_role("sql_gen")
    max_tokens = router.max_tokens_for("sql_gen")

    if use_tools:
        outcome = core.consume(core.run(
            item.question, provider=provider, sandbox=sandbox,
            schema=schema, evidence=item.evidence, max_tokens=max_tokens,
        ))
        if outcome.error:
            return Record(
                qid=item.qid, db_id=item.db_id, question=item.question,
                difficulty=item.difficulty, gold_sql=item.gold_sql, pred_sql="",
                correct=False, executable=False, reason=CALL_FAILED,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                schema_chars=len(schema), error=outcome.error,
                steps=outcome.steps, tool_calls=outcome.tool_calls, hit_cap=outcome.hit_cap,
            )
        sql, u = outcome.sql, outcome.usage
        err = ""
        steps, tcalls, hit_cap = outcome.steps, outcome.tool_calls, outcome.hit_cap
    else:
        try:
            gen = generate_sql(
                provider, dialect=sandbox.dialect,
                schema=schema, question=item.question, evidence=item.evidence,
                max_tokens=max_tokens,
            )
        except LLMError as exc:
            return Record(
                qid=item.qid, db_id=item.db_id, question=item.question,
                difficulty=item.difficulty, gold_sql=item.gold_sql, pred_sql="",
                correct=False, executable=False, reason=CALL_FAILED,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                schema_chars=len(schema), error=str(exc),
            )
        sql, u, err = gen.sql, gen.usage, gen.error
        steps = tcalls = 0
        hit_cap = False

    rec = Record(
        qid=item.qid, db_id=item.db_id, question=item.question,
        difficulty=item.difficulty, gold_sql=item.gold_sql, pred_sql=sql,
        correct=False, executable=False, reason="",
        elapsed_ms=(time.perf_counter() - started) * 1000,
        input_tokens=u.input_tokens, output_tokens=u.output_tokens,
        cached_tokens=u.cached_input_tokens, reasoning_tokens=u.reasoning_tokens,
        cost=u.cost, cost_unit=u.cost_unit, schema_chars=len(schema),
        error=err, steps=steps, tool_calls=tcalls, hit_cap=hit_cap,
    )

    if not rec.pred_sql:
        rec.reason = "没有生成出 SQL"
        return rec
```

（后续 `pred = sandbox.run(rec.pred_sql)` 起原样不动。）

3e. `main` 加开关（放在 `--output-columns` 之后的位置）：

```python
    ap.add_argument("--tools", action="store_true",
                    help="给模型 execute_sql / submit_sql 工具，跑工具循环，ROADMAP 2.4")
```

3f. `run_one` 调用处加参数：

```python
                with_column_values=args.column_values,
                use_tools=args.tools,
```

3g. 汇总行加字段：

```python
            "column_values": args.column_values,
            "use_tools": args.tools,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/test_runner_tools.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 跑全量测试 + 确认老路未变**

```bash
PYTHONIOENCODING=utf-8 uv run --group dev pytest tests/ -q
PYTHONIOENCODING=utf-8 uv run python -m eval.runner --dataset eval/datasets/mini --limit 6 --label mini-check-notools
```
Expected: 测试全绿；mini 自检能跑通（开关默认关，走老路）。数字无意义，只验管线。

- [ ] **Step 6: 提交**（先问用户）

```bash
git add eval/runner.py tests/test_runner_tools.py
git commit -m "2.4 runner 接线：--tools 开关与循环字段"
```

---

### Task 6: 文档同步

**Files:**
- Modify: `docs/DESIGN.md`（3.3 模块表、4.4 事件流）
- Modify: `docs/ROADMAP.md`（2.4 状态）

**Interfaces:** 无代码接口。

- [ ] **Step 1: 改 DESIGN.md 3.3 模块表**

把 `events.py` / `core.py` / `tools.py` 三行的状态从 📋 改为 ✅，职责按实现填写
（`core.py` 写"工具循环，产出事件流；`consume()` 收敛成结果"；`tools.py` 写
"`execute_sql` / `submit_sql` 两个 ToolSpec"）。

- [ ] **Step 2: 改 DESIGN.md 4.4**

去掉"（📋 规划，实现时以此为准）"；把签名更新为实际的
`run(question, *, provider, sandbox, schema, evidence, max_steps, max_tokens)`，
并说明"schema 加载与沙箱选择由调用方负责，循环不开连接"。

- [ ] **Step 3: 改 ROADMAP.md 2.4 行**

状态从 💰 改为 ✅，登记实现内容（两个工具、`max_steps=10` 兜底、`--tools` 默认关），
并注明"**未跑 250 题评测**：靶子约 5 题低于噪声门槛，数字留到阶段 3"。

- [ ] **Step 4: 提交**（先问用户）

```bash
git add docs/DESIGN.md docs/ROADMAP.md
git commit -m "2.4 文档同步：DESIGN 模块表与事件流、ROADMAP 状态"
```

---

## 收尾（不单独成任务）

全部任务完成后，跑 6 题 mini 自检验证端到端：

```bash
PYTHONIOENCODING=utf-8 uv run python -m eval.runner \
    --dataset eval/datasets/mini --limit 6 --tools --label mini-check-tools
```

确认：能跑通、`steps`/`tool_calls`/`hit_cap` 落进结果文件、无异常。
**数字无意义，不得引用**（EVAL 1 节）。250 题正式评测留到阶段 3。
