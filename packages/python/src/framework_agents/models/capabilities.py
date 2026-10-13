"""Model capability registry. Parity: src/models/capabilities.ts.

Researched defaults live here so users never pick tiers. Full per-model
research (vision, tools, reasoning, context limits) lands later; until then:
explicit allowlist + fail-fast error.
"""

from __future__ import annotations

import re

from ..errors import AgentError

# Model ids known to accept image inputs (researched 2026-10; extended as
# models are verified). Mirrors the TS regex table exactly.
VISION_CAPABLE_PATTERNS: tuple[str, ...] = (
    r"qwen2.*vl",
    r"qwen.*vl",
    r"llama-3\.2-11b-vision",
    r"llama-3\.2-90b-vision",
    r"llava",
    r"minicpm-v",
    r"gpt-4o",
    r"gpt-4\.1",
    r"claude.*sonnet",
    r"claude.*opus",
    r"gemini",
)

_COMPILED = tuple(re.compile(pattern, re.IGNORECASE) for pattern in VISION_CAPABLE_PATTERNS)


def supports_vision(model_id: str) -> bool:
    return any(pattern.search(model_id) for pattern in _COMPILED)


def assert_can_handle(model_id: str, needs_vision: bool) -> None:
    """Raise AgentError(MODEL_CAPABILITY) when input needs vision the model lacks."""
    if needs_vision and not supports_vision(model_id):
        raise AgentError(
            "MODEL_CAPABILITY",
            f'Model "{model_id}" cannot take image input. Switch TOGETHER_MODEL to a '
            "vision-capable model (e.g. Qwen/Qwen2.5-VL-72B-Instruct) or drop the images.",
            retryable=False,
        )