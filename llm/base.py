"""与具体厂商无关的 LLM 类型定义。

agent 主循环只针对本模块的类型编写。``llm/`` 之外的任何模块都不应该 import
``anthropic`` 或 ``openai``，也不应该直接操作某家厂商的原生消息格式。
这样换模型只是改一行配置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant"]
StopReason = Literal["end", "tool_use", "max_tokens", "other"]


@dataclass(slots=True)
class ToolCall:
    """模型发起的一次工具调用请求。"""

    id: str
    name: str
    args: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    """工具的执行结果，回灌给模型。"""

    call_id: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class Message:
    """一轮对话的中立表示。

    assistant 轮携带 ``text`` 和/或 ``tool_calls``；user 轮携带 ``text``
    和/或 ``tool_results``。各家格式的差异由适配层负责转换。
    """

    role: Role
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", text=text)

    @classmethod
    def assistant(cls, text: str | None, tool_calls: list[ToolCall] | None = None) -> Message:
        return cls(role="assistant", text=text, tool_calls=tool_calls or [])

    @classmethod
    def results(cls, results: list[ToolResult]) -> Message:
        return cls(role="user", tool_results=results)


@dataclass(slots=True)
class ToolSpec:
    """工具定义：声明一次，按各家格式分别序列化。"""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object


CostUnit = Literal["usd", "credit", "unknown"]


@dataclass(slots=True)
class Usage:
    """单次请求的 token 账目。

    ``cached_input_tokens`` 是 ``input_tokens`` 的子集。之所以单列，是因为各家
    对缓存命中的计价方式都不一样，混在一起算出来的成本没有意义。

    ``reasoning_tokens`` 是 ``output_tokens`` 的子集。推理模型把思考过程按输出
    计费，不单列的话，一次削减思考预算的优化在成本表上看不出任何变化。

    ``cost_unit`` 必须显式声明：按 credit 计费的网关和按美元计费的 API，
    绝不能被静默地加到一起。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    cost_unit: CostUnit = "unknown"
    model: str = ""

    def __add__(self, other: Usage) -> Usage:
        if (
            self.cost_unit != other.cost_unit
            and "unknown" not in (self.cost_unit, other.cost_unit)
        ):
            raise ValueError(
                f"拒绝把 {self.cost_unit} 和 {other.cost_unit} 两种计价单位相加；"
                "请先统一单位再聚合"
            )
        unit = self.cost_unit if self.cost_unit != "unknown" else other.cost_unit
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cost=self.cost + other.cost,
            cost_unit=unit,
            model=self.model if self.model == other.model else "mixed",
        )


@dataclass(slots=True)
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: Usage
    raw: Any = None

    def to_message(self) -> Message:
        return Message.assistant(self.text, self.tool_calls)


class LLMError(RuntimeError):
    """厂商侧的调用失败。调用方可据 ``retryable`` 决定重试还是升级模型。"""

    def __init__(self, message: str, *, provider: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


@runtime_checkable
class LLMProvider(Protocol):
    """agent 主循环唯一允许依赖的接口。"""

    name: str
    model: str

    def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse: ...
