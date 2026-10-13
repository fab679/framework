"""Parity: src/types/index.ts AgentError — code + retryable with stamping."""

from __future__ import annotations

from typing import Any

_MARKER = "framework_retryable"


class AgentError(Exception):
    """Framework wide error: every raise carries a code and a retryable verdict.

    `retryable` is the portable retry contract (TS stampRetryable equivalent):
    network/429/5xx -> True, budget/truncation enforcement -> False.
    """

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"AgentError({self.code!r}, retryable={self.retryable})"


def stamp_retryable(err: BaseException, retryable: bool) -> BaseException:
    """Override a non-AgentError's retry verdict (TS stampRetryable)."""
    setattr(err, _MARKER, retryable)
    return err


def get_retryable(err: BaseException) -> bool | None:
    """Read the retry verdict: explicit stamp > AgentError field > None (unknown)."""
    stamped = getattr(err, _MARKER, None)
    if stamped is not None:
        return bool(stamped)
    if isinstance(err, AgentError):
        return err.retryable
    return None


def _why(exc: BaseException) -> str:
    return str(exc) or exc.__class__.__name__


def summarize(exc: BaseException) -> dict[str, Any]:
    return {"code": exc.code if isinstance(exc, AgentError) else "ERROR", "message": _why(exc)}