"""2.3 输出列约束：system prompt 必须正好是"方言版 + 一条规则"，多一字少一字都不行。

这条规则是 2.3 实验的唯一变量，措辞一动就是另一个实验，所以要钉死。
另外要钉住"默认关闭时走的是 baseline_dialect"——否则已验证的 PG 47.6% 会被悄悄污染。
"""

from __future__ import annotations

from agent import baseline, baseline_dialect
from agent.prompt_columns import EXTRA_RULE, generate_sql, system_prompt
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


class FakeRouter:
    """run_one 只用到 for_role 和 max_tokens_for 两个方法。"""

    def __init__(self, provider: FakeProvider, max_tokens: int = 99) -> None:
        self.provider = provider
        self._max_tokens = max_tokens

    def for_role(self, role: str) -> FakeProvider:
        return self.provider

    def max_tokens_for(self, role: str) -> int:
        return self._max_tokens


def test_sqlite_prompt_is_baseline_plus_rule():
    assert system_prompt("sqlite") == baseline.SYSTEM + "\n" + EXTRA_RULE


def test_postgres_prompt_is_dialect_plus_rule():
    pg = system_prompt("postgres")
    assert pg == baseline_dialect.system_prompt("postgres") + "\n" + EXTRA_RULE
    assert "PostgreSQL" in pg and "SQLite" not in pg


def test_only_the_system_prompt_differs_from_dialect():
    """除了 system，请求的其余部分必须和方言版逐字相同——否则说不清是规则还是别的在起作用。"""
    args = dict(schema="CREATE TABLE t (a INT);", question="多少行？", evidence="e", max_tokens=99)
    a, b = FakeProvider(), FakeProvider()
    baseline_dialect.generate_sql(a, dialect="sqlite", **args)
    r = generate_sql(b, dialect="sqlite", **args)
    assert r.sql == "SELECT 1"
    ca, cb = a.calls[0], b.calls[0]
    assert cb["system"] == system_prompt("sqlite")
    assert {k: v for k, v in ca.items() if k != "system"} == {
        k: v for k, v in cb.items() if k != "system"
    }


def test_sql_extraction_matches_baseline():
    """提取逻辑复用 baseline，连它的怪癖也一致：没有代码块也没有 SELECT 时返回原文。"""
    for reply in ["```sql\nSELECT a FROM t;\n```", "答案是 SELECT 1", "我不知道", ""]:
        r = generate_sql(FakeProvider(reply=reply), dialect="sqlite", schema="", question="?")
        assert r.sql == baseline.extract_sql(reply)


def test_rule_talks_about_columns():
    """规则文字必须真的在讲"列"，否则这个实验就没在测它声称要测的东西。"""
    assert "列" in EXTRA_RULE


# -- runner 接线：默认关闭时必须走原路 -------------------------------------


def _item(db):
    from eval.dataset import Item

    return Item(
        qid="t1", db_id="db", question="有多少行？",
        gold_sql="SELECT 1", evidence="", difficulty="simple", db=db,
    )


def test_runner_default_off_keeps_dialect_prompt(sales_db):
    """output_columns 关着时，发给模型的 system 必须还是方言版原文。"""
    from eval.runner import run_one

    provider = FakeProvider()
    run_one(_item(sales_db), FakeRouter(provider), sample_rows=0, max_rows=2000)
    assert provider.calls[0]["system"] == baseline_dialect.system_prompt("sqlite")


def test_runner_output_columns_switches_prompt(sales_db):
    from eval.runner import run_one

    provider = FakeProvider()
    run_one(
        _item(sales_db), FakeRouter(provider),
        sample_rows=0, max_rows=2000, output_columns=True,
    )
    assert provider.calls[0]["system"] == system_prompt("sqlite")
