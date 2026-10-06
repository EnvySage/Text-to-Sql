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
