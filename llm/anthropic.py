"""Anthropic Messages API 的适配器。

和 OpenAI 格式有三处不同，每一处弄错都会让请求直接失败：

1. ``system`` 是顶层参数，不是一条消息。
2. 工具调用以 ``tool_use`` 块的形式穿插在 ``content`` 里，没有独立字段。
3. 同一轮的所有工具结果要放进**同一条** user 消息，作为 ``tool_result`` 块
   并用 ``tool_use_id`` 关联；而 OpenAI 要求每个结果单独一条消息。
"""

from __future__ import annotations

import os
from typing import Any

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    RateLimitError,
)

from llm.base import (
    LLMError,
    LLMResponse,
    Message,
    StopReason,
    ToolCall,
    ToolSpec,
)
from llm.cost import make_usage

_STOP_REASON: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
}


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str = "claude-sonnet-5",
        api_key: str | None = None,
        api_key_env: str = "ANTHROPIC_API_KEY",
        name: str = "anthropic",
        cache_system: bool = True,
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        key = api_key or os.environ.get(api_key_env)
        if not key:
            raise LLMError(f"缺少 API key：请设置环境变量 ${api_key_env}", provider=name)
        self.name = name
        self.model = model
        # Anthropic 的缓存要逐块显式打标记，DeepSeek 则是自动缓存。
        # 这个不对称性由适配层吞掉，不暴露给 agent 主循环。
        self.cache_system = cache_system
        self._client = Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)

    # -- 序列化 -----------------------------------------------------------

    @staticmethod
    def _to_payload(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            blocks: list[dict[str, Any]] = []

            for r in m.tool_results:
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                )
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            for c in m.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                )

            if blocks:
                out.append({"role": m.role, "content": blocks})
        return out

    @staticmethod
    def _to_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    def _to_system(self, system: str) -> Any:
        if not self.cache_system:
            return system
        return [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # -- 调用 -------------------------------------------------------------

    def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "system": self._to_system(system),
            "messages": self._to_payload(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = self._to_tools(tools)

        try:
            resp = self._client.messages.create(**kwargs)
        except (RateLimitError, APIConnectionError) as exc:
            raise LLMError(str(exc), provider=self.name, retryable=True) from exc
        except APIStatusError as exc:
            raise LLMError(
                f"{exc.status_code}: {exc.message}",
                provider=self.name,
                retryable=exc.status_code >= 500,
            ) from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(
                    ToolCall(id=block.id, name=block.name, args=dict(block.input or {}))
                )

        return LLMResponse(
            text="".join(text_parts) or None,
            tool_calls=calls,
            stop_reason=_STOP_REASON.get(resp.stop_reason or "", "other"),
            usage=_usage(resp, self.model),
            raw=resp,
        )


def _usage(resp: Any, model: str):
    u = resp.usage
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0

    # Anthropic 把缓存 token 报在 input_tokens **之外**，而我们的 Usage 约定是
    # cached_input_tokens 属于 input_tokens 的子集。这里折算回来，
    # 否则跨厂商的成本对比就不是同一个口径。
    return make_usage(
        model,
        input_tokens=(u.input_tokens or 0) + cache_read + cache_write,
        output_tokens=u.output_tokens or 0,
        cached_input_tokens=cache_read,
        reported_cost=None,  # Anthropic 只报 token，不报费用，走本地价格表
    )
