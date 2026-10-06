"""runner 接线：--tools 关时必须走老路，逐字节不变。"""

from __future__ import annotations

from agent import baseline_dialect, core
from eval.dataset import Item
from eval.runner import CALL_FAILED, run_one
from llm.base import LLMError, LLMResponse, ToolCall, Usage


class FakeProvider:
    name = model = "fake"

    def __init__(self, reply="```sql\nSELECT 1\n```"):
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(text=self.reply, tool_calls=[], stop_reason="end",
                           usage=Usage(input_tokens=3, output_tokens=2))


class ScriptedProvider:
    """按脚本依次返回响应；脚本空了就抛 LLMError（模拟循环中途网关挂掉）。"""

    name = model = "fake"

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        if not self.script:
            raise LLMError("网关 503", provider="fake", retryable=True)
        return self.script.pop(0)


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
    p = FakeProvider(reply="")   # 无工具调用 → 循环走兜底收工
    run_one(_item(sales_db), FakeRouter(p), sample_rows=0, max_rows=2000, use_tools=True)
    assert p.calls[0]["system"] == core.system_prompt("sqlite")
    assert [t.name for t in p.calls[0]["tools"]] == ["execute_sql", "submit_sql"]


def test_record_carries_loop_fields(sales_db):
    p = FakeProvider(reply="")
    rec = run_one(_item(sales_db), FakeRouter(p), sample_rows=0, max_rows=2000, use_tools=True)
    assert rec.steps == 1 and rec.tool_calls == 0 and rec.hit_cap is False


def test_call_failure_sets_breaker_and_keeps_usage(sales_db):
    """工具路径上 provider 抛 LLMError：记 CALL_FAILED（熔断哨兵），账不能丢。"""
    # 先试跑一步（花掉 100/50 token），第二步网关挂掉。
    p = ScriptedProvider([
        LLMResponse(text=None, tool_calls=[ToolCall("c1", "execute_sql", {"sql": "SELECT 1"})],
                    stop_reason="tool_use",
                    usage=Usage(input_tokens=100, output_tokens=50, cost=0.5,
                                cost_unit="credit")),
    ])
    rec = run_one(_item(sales_db), FakeRouter(p), sample_rows=0, max_rows=2000,
                  use_tools=True)
    assert rec.reason == CALL_FAILED
    assert "503" in rec.error
    assert rec.steps == 2 and rec.tool_calls == 1
    # 循环中途炸掉时，前面几次调用的 token / cost 也要进账，否则成本表里少一笔钱
    assert rec.input_tokens == 100 and rec.output_tokens == 50
    assert rec.cost == 0.5 and rec.cost_unit == "credit"
