"""主循环：模型调 execute_sql 试跑、调 submit_sql 交卷。全程用假 provider / 假 sandbox。"""

from __future__ import annotations

from agent import core
from llm.base import LLMError, LLMResponse, ToolCall, Usage
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
                           schema="CREATE TABLE t (a INT);", dialect="sqlite", **kw))
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
    assert results, "tool_results 没回灌进 messages"
    assert results[0].tool_results[0].content.startswith("a\n1")
    assert results[0].tool_results[0].is_error is False


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
    assert results, "tool_results 没回灌进 messages"
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


def test_system_prompt_follows_dialect():
    """PG 上跑却把模型当 SQLite 专家，不会报错只会静默写错 SQL——prompt 必须跟方言走。"""
    p = FakeProvider([_resp(calls=[ToolCall("c1", "submit_sql", {"sql": "SELECT 1"})])])
    list(core.run("问题", provider=p, sandbox=FakeSandbox(), schema="",
                  dialect="postgres"))
    assert "PostgreSQL" in p.calls[0]["system"]
    assert "SQLite" not in p.calls[0]["system"]


def test_step_start_carries_reasoning_and_text():
    """界面要显示「它这一步在想什么」，所以思考和文字必须跟着事件一起出去。"""
    r = LLMResponse(
        text="我看看有哪些管理员字段", reasoning="先查数据分布，再决定用哪一组管理员列",
        tool_calls=[ToolCall("c1", "submit_sql", {"sql": "SELECT 1"})],
        stop_reason="tool_use", usage=Usage(),
    )
    events = list(core.run("问题", provider=FakeProvider([r]), sandbox=FakeSandbox(),
                           schema="", dialect="sqlite"))
    assert events[0].type == "step_start"
    assert events[0].payload["reasoning"] == "先查数据分布，再决定用哪一组管理员列"
    assert events[0].payload["text"] == "我看看有哪些管理员字段"


def test_llm_error_converges_to_error_event():
    """provider 抛 LLMError：收敛成 error 事件，不冒泡。"""
    class BoomProvider:
        name = model = "boom"

        def chat(self, **kwargs):
            raise LLMError("网关 503", provider="fake", retryable=True)

    events = list(core.run("问题", provider=BoomProvider(), sandbox=FakeSandbox(),
                           schema="", dialect="sqlite"))
    assert events[-1].type == "error"
    out = core.consume(iter(events))
    assert "503" in out.error
    # error 事件要带上跑到第几步、调过几次工具，否则消费方读到的永远是 0
    assert out.steps == 1 and out.tool_calls == 0


def test_unknown_tool_is_reported_not_crashed():
    """模型喊了不存在的工具：回一条错误结果，循环继续。"""
    script = [
        _resp(calls=[ToolCall("c1", "no_such_tool", {})]),
        _resp(calls=[ToolCall("c2", "submit_sql", {"sql": "SELECT 1"})]),
    ]
    p, events, out = _run(script)
    results = [m for m in p.calls[1]["messages"] if m.tool_results]
    assert results, "tool_results 没回灌进 messages"
    assert results[0].tool_results[0].is_error is True
    assert "未知工具" in results[0].tool_results[0].content
    assert out.sql == "SELECT 1"
    # 未知工具也是一次工具调用，轨迹里要留下 tool_call / tool_result 一对
    assert [e.type for e in events] == [
        "step_start", "tool_call", "tool_result", "step_start", "final",
    ]
