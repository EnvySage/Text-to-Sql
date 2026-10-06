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
