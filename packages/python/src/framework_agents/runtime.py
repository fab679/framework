"""Runtime guard: abort + timeout. Parity: src/runtime/index.ts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .errors import AgentError


@dataclass
class Budget:
    signal: asyncio.Event | None = None  # set = aborted
    timeout_ms: float | None = None


class AbortHandle:
    """TS AbortSignal equivalent: set() to abort in-flight work."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def event(self) -> asyncio.Event:
        return self._event

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def throw_if_aborted(self) -> None:
        if self.aborted:
            raise AgentError("ABORTED", "Run aborted", retryable=False)


async def run_with_budget(fn: Callable[[], Awaitable[Any]], budget: Budget | None = None) -> Any:
    """Abort + timeout race (token/cost budgets live in the agent loop).

    When BOTH a signal and a timeout are given the guard races the two
    branches (FIRST_COMPLETED): the timeout fires even when the signal stays
    unset, and the signal wins when set first."""
    budget = budget or Budget()
    if budget.signal is not None and budget.signal.is_set():
        raise AgentError("ABORTED", "Run aborted", retryable=False)
    if budget.timeout_ms is None and budget.signal is None:
        return await fn()

    async def guard() -> None:
        # Race BOTH guards when both are given: whichever fires first wins
        # (a signal-only branch used to swallow a timeout that never fired).
        branches: list[asyncio.Task[None]] = []

        async def _signal() -> None:
            await budget.signal.wait()  # type: ignore[union-attr]
            raise AgentError("ABORTED", "Run aborted", retryable=False)

        async def _timeout() -> None:
            await asyncio.sleep((budget.timeout_ms or 0) / 1000)
            raise AgentError("TIMEOUT", f"Exceeded {budget.timeout_ms}ms", retryable=True)

        if budget.signal is not None:
            branches.append(asyncio.ensure_future(_signal()))
        if budget.timeout_ms is not None:
            branches.append(asyncio.ensure_future(_timeout()))
        done, pending = await asyncio.wait(set(branches), return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()  # the losing branch must not fire later
        for task in done:
            exc = task.exception()  # raises here if the branch failed to store it
            if exc is not None:
                raise exc

    work = asyncio.ensure_future(fn())
    watchdog = asyncio.ensure_future(guard())
    try:
        # FIRST_COMPLETED: a successful fn() must not wait out the timeout
        # (FIRST_EXCEPTION would, and if the guard won its AgentError re-raises).
        done, _ = await asyncio.wait({work, watchdog}, return_when=asyncio.FIRST_COMPLETED)
        first = next(iter(done))
        return first.result()
    finally:
        for task in (work, watchdog):
            if not task.done():
                task.cancel()  # cancel the loser when the race decided