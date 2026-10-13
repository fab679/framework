"""Per-model prices + cost math. Parity: src/models/pricing.ts (post-fix).

Pinned semantics:
- unknown models -> None (NEVER a misleading zero)
- local ollama: free first, name collisions with cloud models lose to it
- reasoning tokens are already inside output_tokens: never added on top
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .types import TokenUsage as _TokenUsage  # type alias used by call sites

TokenUsage = _TokenUsage


class ModelPrice(BaseModel):
    input_per_million: float
    output_per_million: float


# USD per 1M tokens. Providers send token counts ONLY — cost is always
# computed client-side against a table. Verify against your provider bill.
_PRICES: dict[str, ModelPrice] = {
    "Qwen/Qwen3.5-9B": ModelPrice(input_per_million=0.2, output_per_million=0.8),
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": ModelPrice(input_per_million=0.88, output_per_million=0.88),
    "deepseek-ai/DeepSeek-V4.1-Flash": ModelPrice(input_per_million=0.2, output_per_million=0.6),
    "zai-org/GLM-5.3-Flash": ModelPrice(input_per_million=0.2, output_per_million=0.6),
    "gpt-4o-mini": ModelPrice(input_per_million=0.15, output_per_million=0.6),
    "claude-sonnet-4-6": ModelPrice(input_per_million=3, output_per_million=15),
    "qwen2.5": ModelPrice(input_per_million=0, output_per_million=0),
}

_FREE = ModelPrice(input_per_million=0, output_per_million=0)


def price_for(model_id: str) -> Optional[ModelPrice]:
    """Look up a price by bare model id (`provider:` prefixes are stripped).

    Local ollama first: even a name colliding with a cloud model (e.g.
    `ollama:gpt-4o-mini`) runs free, so it must win over the priced table.
    """
    if model_id.startswith("ollama:"):
        return _FREE
    bare = model_id.split(":", 1)[1] if ":" in model_id else model_id
    return _PRICES.get(bare)


def usage_cost(usage, model_id: str, override: Optional[ModelPrice] = None) -> Optional[float]:
    """USD cost for usage; unknown models return None (never a fake zero).

    `output_tokens` already includes reasoning tokens on every supported
    provider, so they are billed at the output rate with no extra addition.
    """
    price = override or price_for(model_id)
    if price is None:
        return None
    return (
        usage.input_tokens * price.input_per_million + usage.output_tokens * price.output_per_million
    ) / 1_000_000