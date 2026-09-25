"""token 计价。

价格表放在 ``config/pricing.yaml`` 而不是写死在代码里，这样改价不用动 agent。
未知模型返回 0 并警告一次——静默报一个错的数字，比不报数字更糟。
"""

from __future__ import annotations

import warnings
from functools import lru_cache
from pathlib import Path

import yaml

from llm.base import Usage

_PRICING_PATH = Path(__file__).resolve().parent.parent / "config" / "pricing.yaml"
_warned: set[str] = set()


@lru_cache(maxsize=1)
def _table() -> dict[str, dict[str, float]]:
    if not _PRICING_PATH.exists():
        return {}
    return yaml.safe_load(_PRICING_PATH.read_text(encoding="utf-8")) or {}


def price(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """返回美元成本。``cached_input_tokens`` 是 ``input_tokens`` 的子集。"""
    rates = _table().get(model)
    if rates is None:
        if model not in _warned:
            _warned.add(model)
            warnings.warn(
                f"没有 {model!r} 的价格，成本按 0.0 记。"
                f"在信任任何成本指标之前，先把它补进 {_PRICING_PATH.name}。",
                stacklevel=2,
            )
        return 0.0

    fresh_input = max(input_tokens - cached_input_tokens, 0)
    return (
        fresh_input * rates["input"]
        + cached_input_tokens * rates.get("cached_input", rates["input"])
        + output_tokens * rates["output"]
    ) / 1_000_000


def make_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    reasoning_tokens: int = 0,
    reported_cost: float | None = None,
    reported_unit: str = "credit",
) -> Usage:
    """构造一条 Usage 记录。

    当接口自己报了实际扣费（``reported_cost``）时，以它为准，本地价格表退居兜底：
    网关最清楚自己的费率，而我们的表会过期。只报 token 不报费用的厂商才走价格表。
    """
    if reported_cost is not None:
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            reasoning_tokens=reasoning_tokens,
            cost=reported_cost,
            cost_unit=reported_unit,  # type: ignore[arg-type]
            model=model,
        )

    known = model in _table()
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=reasoning_tokens,
        cost=price(model, input_tokens, output_tokens, cached_input_tokens),
        cost_unit="usd" if known else "unknown",
        model=model,
    )
