"""OpenAI 兼容接口的适配器。

覆盖 OpenAI 本身、DeepSeek、Kimi、Qwen、本地 vLLM 以及各类网关——
它们之间只差一个 ``base_url`` 和 ``model``。
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

from llm.base import (
    LLMError,
    LLMResponse,
    Message,
    StopReason,
    ToolCall,
    ToolSpec,
)
from llm.cost import make_usage

_FINISH_REASON: dict[str, StopReason] = {
    "stop": "end",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
}

PRESETS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "kimi": "https://api.moonshot.cn/v1",
    # 从 $LOCAL_BASE_URL 读取，网关换地址不用改代码
    "local": os.environ.get("LOCAL_BASE_URL", "http://127.0.0.1:7863/v1"),
}


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        name: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 5,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        resolved_url = PRESETS.get(base_url or "", base_url)
        key = api_key or os.environ.get(api_key_env)
        if not key:
            raise LLMError(
                f"缺少 API key：请设置环境变量 ${api_key_env}",
                provider=name or "openai-compat",
            )
        self.name = name or (base_url or "openai")
        self.model = model
        # 各家私有的开关（reasoning_effort、thinking 等）通过配置注入，
        # 不写死在代码里——换模型时这些参数往往要跟着换。
        self.extra_params = dict(extra_params or {})
        self.max_retries = max_retries
        self._client = OpenAI(
            api_key=key,
            base_url=resolved_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    # -- 序列化 -----------------------------------------------------------

    def _to_payload(self, system: str, messages: list[Message]) -> list[dict[str, Any]]:
        """转成 OpenAI 的消息格式。

        两个和 Anthropic 不同的地方：system prompt 作为第一条消息传入；
        每个工具结果都是独立的一条 ``role="tool"`` 消息，而 Anthropic 是
        把它们合并进同一条 user 消息。
        """
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.tool_results:
                for r in m.tool_results:
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": r.call_id,
                            "content": r.content,
                        }
                    )
                if m.text:
                    out.append({"role": "user", "content": m.text})
                continue

            if m.role == "assistant" and m.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": m.text or None,
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {
                                    "name": c.name,
                                    "arguments": json.dumps(c.args, ensure_ascii=False),
                                },
                            }
                            for c in m.tool_calls
                        ],
                    }
                )
                continue

            out.append({"role": m.role, "content": m.text or ""})
        return out

    @staticmethod
    def _to_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
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
            "messages": self._to_payload(system, messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = self._to_tools(tools)
            kwargs["tool_choice"] = "auto"

        for k, v in self.extra_params.items():
            if k == "extra_body":
                kwargs.setdefault("extra_body", {}).update(v)
            else:
                kwargs[k] = v

        resp = self._create_with_backoff(kwargs)

        choice = resp.choices[0]
        msg = choice.message

        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    args=_loads(tc.function.arguments),
                )
            )

        return LLMResponse(
            text=msg.content,
            tool_calls=calls,
            stop_reason=_FINISH_REASON.get(choice.finish_reason or "", "other"),
            usage=_usage(resp, self.model),
            raw=resp,
        )


    def _create_with_backoff(self, kwargs: dict[str, Any]) -> Any:
        """带指数退避的调用。

        网关在并发压力下会返回 503 no_healthy_account——账号池被打满，
        过几秒就好了。SDK 自带的重试不够密，评测跑到一半大面积失败会让
        准确率凭空掉十几个点，而那和模型能力毫无关系。

        加抖动是因为并发的多个 worker 同时被拒后，固定退避会让它们
        在同一时刻一起重试，等于没退避。
        """
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._client.chat.completions.create(**kwargs)
            except (RateLimitError, APIConnectionError) as exc:
                last = exc
            except APIStatusError as exc:
                if exc.status_code < 500 and exc.status_code != 429:
                    raise LLMError(
                        f"{exc.status_code}: {exc.message}",
                        provider=self.name, retryable=False,
                    ) from exc
                last = exc
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 16) * (0.5 + random.random()))

        raise LLMError(
            f"重试 {self.max_retries} 次后仍失败：{last}",
            provider=self.name, retryable=True,
        ) from last


def _loads(raw: str | None) -> dict[str, Any]:
    """解析工具调用的参数。

    参数是以 JSON 字符串传回来的，小模型确实会吐出不合法的 JSON。
    这里不抛异常，而是把原文和报错一起带出去，让 agent 主循环能把错误
    回灌给模型让它自己改，而不是整轮跑崩。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"__parse_error__": str(exc), "__raw__": raw}
    return parsed if isinstance(parsed, dict) else {"__raw__": parsed}


def _usage(resp: Any, model: str):
    u = getattr(resp, "usage", None)
    if u is None:
        return make_usage(model, 0, 0, reported_cost=None)

    raw: dict[str, Any] = u.model_dump() if hasattr(u, "model_dump") else dict(u)

    # 缓存命中的字段名在野外有三种写法：
    #   DeepSeek / 本地网关   -> prompt_cache_hit_tokens
    #   OpenAI               -> prompt_tokens_details.cached_tokens
    #   Anthropic 风格         -> cache_read_input_tokens
    cached = (
        raw.get("prompt_cache_hit_tokens")
        or (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
        or raw.get("cache_read_input_tokens")
        or 0
    )

    # 推理模型把思考过程按输出 token 计费。不单独统计的话，
    # 一次削减思考预算的路由改动看起来会像什么都没发生。
    reasoning = (
        (raw.get("completion_tokens_details") or {}).get("reasoning_tokens")
        or raw.get("completion_thinking_tokens")
        or 0
    )

    # 接口自己报了扣费就以它为准，不用本地价格表估算。
    reported = raw.get("credit")
    if reported is None:
        reported = raw.get("cost")

    return make_usage(
        model,
        input_tokens=raw.get("prompt_tokens") or 0,
        output_tokens=raw.get("completion_tokens") or 0,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
        reported_cost=float(reported) if reported is not None else None,
        reported_unit="credit",
    )
