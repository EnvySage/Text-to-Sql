"""两套序列化格式的离线校验。

不需要 API key，不需要网络。存在的意义：工具结果的回灌格式是唯一一处
写错了会让一家直接报 400、另一家静默给出错误答案的地方。
"""

from __future__ import annotations

import json

import pytest

from llm.anthropic import AnthropicProvider
from llm.base import Message, ToolCall, ToolResult, ToolSpec
from llm.cost import make_usage, price
from llm.openai_compat import OpenAICompatProvider

TOOL = ToolSpec(
    name="run_sql",
    description="执行一条只读 SQL 查询。",
    parameters={
        "type": "object",
        "properties": {"sql": {"type": "string"}},
        "required": ["sql"],
    },
)

# 一轮完整的工具调用对话：提问 -> 模型调工具 -> 回灌结果
CONVO = [
    Message.user("how many orders?"),
    Message.assistant(
        "checking",
        [ToolCall(id="call_1", name="run_sql", args={"sql": "SELECT COUNT(*) FROM orders"})],
    ),
    Message.results([ToolResult(call_id="call_1", content="41823")]),
]


@pytest.fixture
def oai():
    return OpenAICompatProvider(model="deepseek-chat", base_url="deepseek", api_key="x")


@pytest.fixture
def ant():
    return AnthropicProvider(model="claude-sonnet-5", api_key="x")


def test_openai_puts_system_first(oai):
    """OpenAI 的 system prompt 是消息列表的第一条。"""
    payload = oai._to_payload("SYS", CONVO)
    assert payload[0] == {"role": "system", "content": "SYS"}


def test_openai_tool_result_is_its_own_message(oai):
    """OpenAI 每个工具结果单独一条 role="tool" 消息，靠 tool_call_id 关联。"""
    payload = oai._to_payload("SYS", CONVO)
    tool_msgs = [m for m in payload if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"


def test_openai_tool_call_arguments_are_json_strings(oai):
    """OpenAI 的工具参数必须是 JSON 字符串，不是 dict。写成 dict 会被拒。"""
    payload = oai._to_payload("SYS", CONVO)
    asst = next(m for m in payload if m["role"] == "assistant")
    args = asst["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str)
    assert json.loads(args) == {"sql": "SELECT COUNT(*) FROM orders"}


def test_anthropic_groups_results_into_one_user_turn(ant):
    """Anthropic 的工具结果合并进同一条 user 消息，作为 tool_result 块。"""
    payload = ant._to_payload(CONVO)
    last = payload[-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["tool_use_id"] == "call_1"


def test_anthropic_tool_use_input_stays_a_dict(ant):
    """和 OpenAI 相反，Anthropic 的工具参数是 dict，不能序列化成字符串。"""
    payload = ant._to_payload(CONVO)
    asst = next(m for m in payload if m["role"] == "assistant")
    block = next(b for b in asst["content"] if b["type"] == "tool_use")
    assert block["input"] == {"sql": "SELECT COUNT(*) FROM orders"}


def test_anthropic_never_emits_a_system_message(ant):
    """Anthropic 的 system 是顶层参数，混进 messages 里会报错。"""
    payload = ant._to_payload(CONVO)
    assert all(m["role"] != "system" for m in payload)


def test_tool_schema_key_differs_per_provider(oai, ant):
    """同一个工具定义，两家的 JSON Schema 字段名不同。"""
    assert "parameters" in oai._to_tools([TOOL])[0]["function"]
    assert "input_schema" in ant._to_tools([TOOL])[0]


def test_malformed_tool_arguments_are_surfaced_not_raised():
    """小模型会吐出坏 JSON。这里要能把错误带出来，而不是让整轮跑崩。"""
    from llm.openai_compat import _loads

    out = _loads('{"sql": "SELECT')
    assert "__parse_error__" in out
    assert out["__raw__"] == '{"sql": "SELECT'


def test_cached_tokens_are_cheaper_than_fresh_ones():
    """缓存命中的 input token 必须比新 token 便宜，否则缓存统计没意义。"""
    fresh = price("deepseek-chat", input_tokens=1_000_000, output_tokens=0)
    cached = price(
        "deepseek-chat",
        input_tokens=1_000_000,
        output_tokens=0,
        cached_input_tokens=1_000_000,
    )
    assert cached < fresh


def test_unknown_model_costs_zero_and_warns():
    """未知模型返回 0 并警告——静默编一个数字比不给数字更糟。"""
    with pytest.warns(UserWarning, match="没有"):
        assert price("made-up-model", 1000, 1000) == 0.0


def test_gateway_reported_credit_beats_local_price_table():
    """网关最清楚自己的费率，本地价格表会过期，以接口上报的为准。"""
    u = make_usage("deepseek-chat", 1000, 500, reported_cost=0.01)
    assert u.cost == 0.01
    assert u.cost_unit == "credit"


def test_price_table_is_used_when_nothing_is_reported():
    """接口不报费用时才退回本地价格表。"""
    u = make_usage("deepseek-chat", 1_000_000, 0, reported_cost=None)
    assert u.cost > 0
    assert u.cost_unit == "usd"


def test_reasoning_tokens_are_tracked_apart_from_output():
    """思考 token 要单列，否则削减思考预算的优化在账面上看不出来。"""
    u = make_usage("deepseek-chat", 100, 500, reasoning_tokens=300, reported_cost=0.0)
    assert u.output_tokens == 500 and u.reasoning_tokens == 300


def test_credits_and_dollars_refuse_to_be_summed():
    """两种计价单位混加出来的成本表毫无意义，宁可抛异常。"""
    credit = make_usage("m", 1, 1, reported_cost=0.01)
    dollars = make_usage("deepseek-chat", 1, 1, reported_cost=None)
    with pytest.raises(ValueError, match="拒绝"):
        _ = credit + dollars


def test_usage_addition_accumulates_every_column():
    """相加时每一列都要累积，漏一列后面的成本分析就是错的。"""
    a = make_usage("m", 10, 20, cached_input_tokens=5, reasoning_tokens=3, reported_cost=0.1)
    b = make_usage("m", 1, 2, cached_input_tokens=1, reasoning_tokens=1, reported_cost=0.2)
    t = a + b
    assert (t.input_tokens, t.output_tokens) == (11, 22)
    assert (t.cached_input_tokens, t.reasoning_tokens) == (6, 4)
    assert round(t.cost, 6) == 0.3
