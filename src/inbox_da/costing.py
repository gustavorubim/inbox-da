"""Token usage aggregation and cost estimation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GEMINI_3_FLASH_PREVIEW_INPUT_PER_1M_USD = 0.50
GEMINI_3_FLASH_PREVIEW_OUTPUT_PER_1M_USD = 3.00
GEMINI_3_FLASH_PRICING_SOURCE = "https://ai.google.dev/pricing"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            model_calls=self.model_calls + other.model_calls,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "model_calls": self.model_calls,
        }


@dataclass(frozen=True)
class CostEstimate:
    input_cost: float
    output_cost: float
    total_cost: float
    currency: str
    input_price_per_1m: float
    output_price_per_1m: float
    pricing_source: str

    def as_dict(self) -> dict[str, float | str]:
        return {
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "total_cost": self.total_cost,
            "currency": self.currency,
            "input_price_per_1m": self.input_price_per_1m,
            "output_price_per_1m": self.output_price_per_1m,
            "pricing_source": self.pricing_source,
        }


def estimate_cost(
    usage: TokenUsage,
    *,
    input_price_per_1m: float = GEMINI_3_FLASH_PREVIEW_INPUT_PER_1M_USD,
    output_price_per_1m: float = GEMINI_3_FLASH_PREVIEW_OUTPUT_PER_1M_USD,
    currency: str = "USD",
    pricing_source: str = GEMINI_3_FLASH_PRICING_SOURCE,
) -> CostEstimate:
    input_cost = usage.input_tokens * input_price_per_1m / 1_000_000
    output_cost = usage.output_tokens * output_price_per_1m / 1_000_000
    return CostEstimate(
        input_cost=round(input_cost, 8),
        output_cost=round(output_cost, 8),
        total_cost=round(input_cost + output_cost, 8),
        currency=currency,
        input_price_per_1m=input_price_per_1m,
        output_price_per_1m=output_price_per_1m,
        pricing_source=pricing_source,
    )


def collect_token_usage(value: Any) -> TokenUsage:
    seen: set[int] = set()
    return _collect_token_usage(value, seen)


def _collect_token_usage(value: Any, seen: set[int]) -> TokenUsage:
    if id(value) in seen:
        return TokenUsage()
    seen.add(id(value))

    usage = _usage_from_object(value)
    if usage.model_calls:
        return usage

    if isinstance(value, dict):
        direct = _usage_from_mapping(value)
        if direct.model_calls:
            return direct
        total = TokenUsage()
        for item in value.values():
            total += _collect_token_usage(item, seen)
        return total

    if isinstance(value, list | tuple):
        total = TokenUsage()
        for item in value:
            total += _collect_token_usage(item, seen)
        return total

    return TokenUsage()


def _usage_from_object(value: Any) -> TokenUsage:
    usage_metadata = getattr(value, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        usage = _usage_from_mapping(usage_metadata)
        if usage.model_calls:
            return usage

    response_metadata = getattr(value, "response_metadata", None)
    if isinstance(response_metadata, dict):
        for key in ("usage_metadata", "token_usage", "usage"):
            nested = response_metadata.get(key)
            if isinstance(nested, dict):
                usage = _usage_from_mapping(nested)
                if usage.model_calls:
                    return usage
        usage = _usage_from_mapping(response_metadata)
        if usage.model_calls:
            return usage
    return TokenUsage()


def _usage_from_mapping(value: dict[str, Any]) -> TokenUsage:
    input_tokens = _first_int(
        value,
        "input_tokens",
        "prompt_tokens",
        "prompt_token_count",
        "input_token_count",
    )
    output_tokens = _first_int(
        value,
        "output_tokens",
        "completion_tokens",
        "candidates_token_count",
        "output_token_count",
    )
    total_tokens = _first_int(value, "total_tokens", "total_token_count")
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    if input_tokens == output_tokens == total_tokens == 0:
        return TokenUsage()
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        model_calls=1,
    )


def _first_int(value: dict[str, Any], *keys: str) -> int:
    for key in keys:
        item = value.get(key)
        if isinstance(item, int | float):
            return int(item)
    return 0
