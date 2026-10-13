"""Local-first Ollama provider. Parity: src/models/ollama.ts.

Via Ollama's OpenAI-compatible endpoint: no API key, no account, no network
beyond localhost. Tool support depends on the model (qwen2.5, llama3.1+ work
well). structured_output stays False -> `auto` format uses the tool strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .openai_compatible import OpenAICompatibleOptions, OpenAICompatibleProvider


@dataclass
class OllamaOptions:
    model: str
    # Defaults to local Ollama. No key needed.
    base_url: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = None
    fetch_fn: Optional[Callable[[], Any]] = None


class OllamaProvider(OpenAICompatibleProvider):
    id: str
    capabilities = {"structured_output": False, "streaming": True}

    def __init__(self, opts: OllamaOptions) -> None:
        super().__init__(
            OpenAICompatibleOptions(
                base_url=opts.base_url or "http://localhost:11434/v1",
                api_key="ollama",
                model=opts.model,
                temperature=opts.temperature if opts.temperature is not None else 0.2,
                max_tokens=opts.max_tokens,
                stream=bool(opts.stream),
                fetch_fn=opts.fetch_fn,
            )
        )
        self.id = f"ollama:{opts.model}"


def ollama(model: str, **kwargs: Any) -> OllamaProvider:
    return OllamaProvider(OllamaOptions(model=model, **kwargs))