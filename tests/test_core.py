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


def test_conclude_sends_sql_and_result_to_the_model():
    """结论的数字只能来自给出去的结果——所以 prompt 里必须同时有 SQL 和结果。"""
    p = FakeProvider([_resp(text="一共有 180 条记录。")])
    text, _ = core.conclude(
        "有多少条记录？", "SELECT count(*) FROM records", "count\n180", provider=p,
    )
    assert text == "一共有 180 条记录。"
    prompt = p.calls[0]["messages"][0].text
    assert "SELECT count(*) FROM records" in prompt and "180" in prompt
    assert p.calls[0]["tools"] is None      # 结论步骤不调工具


def test_conclude_skips_without_a_result():
    """SQL 没产出、或结果为空时不调模型——没数据可依据，硬问只会让它编。"""
    p = FakeProvider([_resp(text="不该被调用")])
    assert core.conclude("问", "SELECT 1", "", provider=p)[0] == ""
    assert core.conclude("问", "", "count\n1", provider=p)[0] == ""
    assert p.calls == []


def test_conclude_swallows_llm_error():
    """结论失败不该炸掉整轮——返回空串，主流程照常显示 SQL 和结果。"""
    class Boom:
        name = model = "boom"

        def chat(self, **kwargs):
            raise LLMError("网关挂了", provider="fake")

    assert core.conclude("问", "SELECT 1", "count\n1", provider=Boom())[0] == ""


def test_ask_user_tool_only_offered_when_callback_given():
    """评测路径的工具集必须逐字节不变——不给 ask 就不挂 ask_user。"""
    base = dict(sandbox=FakeSandbox(), schema="", dialect="sqlite")
    p1 = FakeProvider([_resp(calls=[ToolCall("c1", "submit_sql", {"sql": "SELECT 1"})])])
    list(core.run("问题", provider=p1, **base))
    assert [t.name for t in p1.calls[0]["tools"]] == ["execute_sql", "submit_sql"]

    p2 = FakeProvider([_resp(calls=[ToolCall("c1", "submit_sql", {"sql": "SELECT 1"})])])
    list(core.run("问题", provider=p2, ask=lambda a: "答", **base))
    assert "ask_user" in [t.name for t in p2.calls[0]["tools"]]


def test_ask_user_feeds_the_answer_back_to_the_model():
    asked = []

    def _ask(args):
        asked.append(args)
        return "用户确认：复购 = 同一客户下单超过一次"

    script = [
        _resp(calls=[ToolCall("c1", "ask_user", {
            "term": "复购", "question": "复购是指什么？",
            "candidates": ["同一客户下单超过一次", "同一店铺下单超过一次"]})]),
        _resp(calls=[ToolCall("c2", "submit_sql", {"sql": "SELECT 1"})]),
    ]
    p = FakeProvider(script)
    list(core.run("复购率是多少", provider=p, sandbox=FakeSandbox(), schema="",
                  dialect="sqlite", ask=_ask))
    assert asked and asked[0]["term"] == "复购"
    results = [m for m in p.calls[1]["messages"] if m.tool_results]
    assert "同一客户下单超过一次" in results[0].tool_results[0].content


def test_ask_user_without_a_user_degrades_and_continues():
    """用户不在场时必须让它继续——卡着等一个不会来的回答，比猜错更糟。"""
    script = [
        _resp(calls=[ToolCall("c1", "ask_user", {"term": "复购", "question": "?"})]),
        _resp(calls=[ToolCall("c2", "submit_sql", {"sql": "SELECT 1"})]),
    ]
    p = FakeProvider(script)
    events = list(core.run("复购率", provider=p, sandbox=FakeSandbox(), schema="",
                           dialect="sqlite"))          # 不给 ask
    results = [m for m in p.calls[1]["messages"] if m.tool_results]
    assert "用户不在场" in results[0].tool_results[0].content
    assert core.consume(iter(events)).sql == "SELECT 1"


def test_ask_user_caps_the_number_of_questions():
    """问多了烦人，也说明 prompt 没讲清楚。超了就直接回「不能再问」。"""
    hits = []

    def _ask(args):
        hits.append(1)
        return "答"

    script = [
        _resp(calls=[ToolCall(f"c{i}", "ask_user", {"term": f"t{i}", "question": "?"})])
        for i in range(core.MAX_ASKS + 2)
    ]
    script.append(_resp(calls=[ToolCall("c9", "submit_sql", {"sql": "SELECT 1"})]))
    p = FakeProvider(script)
    events = list(core.run("问", provider=p, sandbox=FakeSandbox(), schema="",
                           dialect="sqlite", ask=_ask))
    assert len(hits) == core.MAX_ASKS
    assert core.consume(iter(events)).sql == "SELECT 1"


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
