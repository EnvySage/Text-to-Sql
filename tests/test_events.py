"""事件类型：主循环产出、CLI/Web/runner 消费的公共契约。"""

from __future__ import annotations

import time

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
    """每个事件各自取一次时间戳：同一时刻创建的两个事件不能共享 ts。"""
    e1 = AgentEvent("step_start")
    time.sleep(0.01)
    e2 = AgentEvent("step_start")
    assert e1.ts < e2.ts
