"""few-shot 版生成：示例进 prompt，其余一字不动。"""

from __future__ import annotations

from agent import baseline, baseline_dialect
from agent.prompt_fewshot import generate_sql
from llm.base import LLMResponse, Usage
from retrieval.fewshot import Example


class FakeProvider:
    name = model = "fake"

    def __init__(self, reply: str = "```sql\nSELECT 1\n```") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(text=self.reply, tool_calls=[], stop_reason="end", usage=Usage())


ARGS = dict(schema="CREATE TABLE t (a INT);", question="多少行？", evidence="e", max_tokens=99)


def test_no_examples_is_identical_to_dialect():
    """没有示例时，请求必须和 baseline_dialect 逐字节相同——否则说不清是示例还是别的在起作用。"""
    a, b = FakeProvider(), FakeProvider()
    baseline_dialect.generate_sql(a, dialect="sqlite", **ARGS)
    generate_sql(b, dialect="sqlite", examples=[], **ARGS)
    assert a.calls == b.calls


def test_examples_appear_in_the_user_prompt():
    p = FakeProvider()
    ex = [Example("示例问题？", "SELECT a FROM t", "db")]
    generate_sql(p, dialect="sqlite", examples=ex, **ARGS)
    prompt = p.calls[0]["messages"][0].text
    assert "示例问题？" in prompt and "SELECT a FROM t" in prompt
    assert "多少行？" in prompt   # 原问题仍在


def test_examples_do_not_touch_the_system_prompt():
    """示例只进 user 消息，system 还是方言版原文。"""
    p = FakeProvider()
    generate_sql(p, dialect="postgres", examples=[Example("q", "SELECT 1", "d")], **ARGS)
    assert p.calls[0]["system"] == baseline_dialect.system_prompt("postgres")


def test_sql_extraction_matches_baseline():
    for reply in ["```sql\nSELECT a FROM t;\n```", "答案是 SELECT 1", "我不知道", ""]:
        r = generate_sql(FakeProvider(reply=reply), dialect="sqlite", schema="", question="?")
        assert r.sql == baseline.extract_sql(reply)
