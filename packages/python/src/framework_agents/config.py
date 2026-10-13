"""Env config + provider factory. Parity: src/config + src/models/factory.

Walk-up .env loading (cwd, then 3 parents), TogetherAI preferred when its
key is set — same defaults as TS loadConfig/initChatModel.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import ModelProvider
from .models.openai_compatible import OpenAICompatibleOptions, OpenAICompatibleProvider
from .models.anthropic import AnthropicOptions, AnthropicProvider
from .models.ollama import OllamaOptions, OllamaProvider


def parse_env_file(text: str) -> dict[str, str]:
    """Parse .env text into key/value pairs.

    Divergences fixed against the classic dotenv loaders (TS parity checks):
    - ``export KEY=v`` (sh-style export prefix) is supported
    - unquoted trailing comments are stripped: ``KEY=v # note`` -> ``v``
    - duplicate keys within ONE file: LAST wins (the real process env still
      wins over every file value — callers apply these with setdefault)
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
            if "=" not in line:
                continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1]  # fully quoted: keep everything inside
        elif " #" in value:
            # Unquoted trailing comment: only " #" (hash after whitespace) so
            # "a#b" values and quoted "# within value" stay intact.
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _try_setuptools_free_dotenv() -> None:  # minimal, dependency-free
    import os

    def load_env_file(path: Path) -> None:
        try:
            for key, value in parse_env_file(path.read_text(encoding="utf-8")).items():
                # setdefault: real process env always wins over file values.
                os.environ.setdefault(key, value)
        except OSError:
            pass

    override = os.environ.get("FRAMEWORK_ENV_PATH")
    if override:
        load_env_file(Path(override))
        return
    load_env_file(Path.cwd() / ".env")
    node = Path.cwd()
    for _ in range(3):
        node = node.parent
        candidate = node / ".env"
        if candidate.exists():
            load_env_file(candidate)


_ENV_LOADED = False


def _ensure_env() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        _try_setuptools_free_dotenv()
        _ENV_LOADED = True


_DEFAULTS = {
    "TOGETHER_BASE_URL": "https://api.together.xyz/v1",
    "TOGETHER_MODEL": "Qwen/Qwen3.5-9B",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "OPENAI_MODEL": "gpt-4o-mini",
    "OLLAMA_BASE_URL": "http://localhost:11434/v1",
    "OLLAMA_MODEL": "qwen2.5",
}


@dataclass
class Config:
    together_api_key: str
    together_base_url: str
    together_model: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    anthropic_api_key: str
    anthropic_base_url: str
    anthropic_model: str
    ollama_base_url: str
    ollama_model: str

    @property
    def active(self) -> tuple[str, str, str, str]:
        """(source, base_url, api_key, model) — Together wins when keyed."""
        import os

        if self.together_api_key:
            return ("together", self.together_base_url, self.together_api_key, self.together_model)
        return ("openai", self.openai_base_url, self.openai_api_key, self.openai_model)


_cache: Config | None = None


def load_config() -> Config:
    global _cache
    if _cache is None:
        _ensure_env()
        import os

        _cache = Config(
            together_api_key=os.environ.get("TOGETHER_API_KEY", ""),
            together_base_url=os.environ.get("TOGETHER_BASE_URL", _DEFAULTS["TOGETHER_BASE_URL"]),
            together_model=os.environ.get("TOGETHER_MODEL", _DEFAULTS["TOGETHER_MODEL"]),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", _DEFAULTS["OPENAI_BASE_URL"]),
            openai_model=os.environ.get("OPENAI_MODEL", _DEFAULTS["OPENAI_MODEL"]),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            ollama_base_url=os.environ.get("OLLAMA_BASE_URL", _DEFAULTS["OLLAMA_BASE_URL"]),
            ollama_model=os.environ.get("OLLAMA_MODEL", _DEFAULTS["OLLAMA_MODEL"]),
        )
    return _cache


def init_chat_model(
    spec: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    max_tokens: int | None = None,
    stream: bool = False,
    thinking_budget: int | None = None,
) -> ModelProvider:
    """Resolve `provider:model` strings (bare ids default to together)."""
    cfg = load_config()
    provider, _, model = (spec if ":" in spec else f"together:{spec}").partition(":")
    extra = OpenAICompatibleOptions(
        base_url=base_url or "",
        api_key=api_key or "",
        model=model,
        max_tokens=max_tokens,
        stream=stream,
    )
    if provider == "together":
        key = api_key or cfg.together_api_key
        if not key:
            raise ValueError("init_chat_model: set TOGETHER_API_KEY in .env (or pass api_key)")
        return OpenAICompatibleProvider(
            OpenAICompatibleOptions(base_url=base_url or cfg.together_base_url, api_key=key, model=model,
                                    max_tokens=max_tokens, stream=stream)
        )
    if provider == "openai":
        key = api_key or cfg.openai_api_key
        if not key:
            raise ValueError("init_chat_model: set OPENAI_API_KEY in .env (or pass api_key)")
        return OpenAICompatibleProvider(
            OpenAICompatibleOptions(base_url=base_url or cfg.openai_base_url, api_key=key, model=model,
                                    max_tokens=max_tokens, stream=stream)
        )
    if provider == "anthropic":
        key = api_key or cfg.anthropic_api_key
        if not key:
            raise ValueError("init_chat_model: set ANTHROPIC_API_KEY in .env (or pass api_key)")
        return AnthropicProvider(AnthropicOptions(
            api_key=key,
            model=model,
            base_url=base_url or cfg.anthropic_base_url,
            max_tokens=max_tokens if max_tokens is not None else 1024,
            stream=stream,
            thinking_budget=thinking_budget,
        ))
    if provider == "ollama":
        return OllamaProvider(OllamaOptions(
            model=model,
            base_url=base_url or cfg.ollama_base_url,
            max_tokens=max_tokens,
            stream=stream,
        ))
    if provider == "custom":
        if not base_url or not api_key:
            raise ValueError("init_chat_model: custom: needs base_url + api_key")
        return OpenAICompatibleProvider(extra)
    raise ValueError(f'init_chat_model: unknown provider "{provider}" (use together:|openai:|anthropic:|ollama:|custom:)')