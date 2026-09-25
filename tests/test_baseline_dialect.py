"""方言版 baseline：跨方言对比时，prompt 只能差方言名这一处。"""

from __future__ import annotations

from agent import baseline
from agent.baseline_dialect import generate_sql, system_prompt
from llm.base import LLMResponse, Usage


class FakeProvider:
    """记下每次调用的参数，不发网络请求。"""

    name = "fake"
    model = "fake"

    def __init__(self, reply: str = "```sql\nSELECT 1\n```") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(text=self.reply, tool_calls=[], stop_reason="end", usage=Usage())


def test_sqlite_prompt_is_exactly_baseline():
    assert system_prompt("sqlite") == baseline.SYSTEM


def test_postgres_prompt_differs_only_in_dialect_name():
    pg = system_prompt("postgres")
    assert "PostgreSQL" in pg and "SQLite" not in pg
    assert pg.replace("PostgreSQL", "SQLite") == baseline.SYSTEM


def test_sqlite_call_is_identical_to_baseline():
    """SQLite 路径必须和直接调 baseline 发出完全相同的请求，否则已有数字的可比性就断了。"""
    args = dict(schema="CREATE TABLE t (a INT);", question="多少行？", evidence="e", max_tokens=99)
    a, b = FakeProvider(), FakeProvider()
    baseline.generate_sql(a, **args)
    generate_sql(b, dialect="sqlite", **args)
    assert a.calls == b.calls


def test_postgres_call_changes_only_system_prompt():
    args = dict(schema="CREATE TABLE t (a INT);", question="多少行？", evidence="e", max_tokens=99)
    a, b = FakeProvider(), FakeProvider()
    baseline.generate_sql(a, **args)
    r = generate_sql(b, dialect="postgres", **args)
    assert r.sql == "SELECT 1"
    ca, cb = a.calls[0], b.calls[0]
    assert cb["system"] == system_prompt("postgres")
    assert {k: v for k, v in ca.items() if k != "system"} == {k: v for k, v in cb.items() if k != "system"}


def test_sql_extraction_matches_baseline():
    """提取逻辑直接复用 baseline，连它的怪癖也一致：没有代码块也没有 SELECT 时返回原文而不是空串。"""
    for reply in ["```sql\nSELECT a FROM t;\n```", "答案是 SELECT 1", "我不知道", ""]:
        r = generate_sql(FakeProvider(reply=reply), dialect="postgres", schema="", question="?")
        assert r.sql == baseline.extract_sql(reply)
