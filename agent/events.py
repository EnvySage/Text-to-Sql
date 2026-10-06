"""agent 主循环产出的事件流。

主循环只产出事件，不做任何渲染。CLI、Web、轨迹存储、评测 runner 都是消费者——
一套引擎可以同时被人看、被存、被回放、被评测。见 docs/DESIGN.md 4.4。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from llm.base import Usage

EventType = Literal[
    "step_start", "tool_call", "tool_result", "final", "error",
    # 阶段 3 预留：planner / verifier / 重试
    "plan", "verify", "retry",
]


@dataclass(slots=True)
class AgentEvent:
    """一次循环产出的单个事件。``payload`` 放类型相关的数据，``usage`` 只在该事件
    消耗了模型调用时带上。"""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    usage: Usage | None = None
    ts: float = field(default_factory=time.time)
