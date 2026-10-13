"""DAG workflows. Parity: packages/typescript/src/workflows/index.ts.

Topological levels run in order; nodes within a level run concurrently.
Duplicate depends entries ("a", "a") are ONE edge — counting them twice
wedges indegree at 1 and misreports a legal graph as a cycle (pinned TS
regression). Abort exceptions always rethrow, never retry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .errors import AgentError


@dataclass
class RetryPolicy:
    attempts: int = 1
    backoff_ms: float = 0


@dataclass
class WorkflowNode:
    id: str
    run: Callable[[dict[str, Any], Optional[dict[str, Any]]], Awaitable[Any]]
    depends_on: list[str] = field(default_factory=list)
    """Retry policy. Default: no retry (attempts=1). Abort always rethrows."""
    retry: Optional[RetryPolicy] = None


@dataclass
class WorkflowOptions:
    """Shared per-run data passed to every node (never sent to models by itself)."""
    invocation_state: Optional[dict[str, Any]] = None


def _sort_topo(nodes: list[WorkflowNode]) -> list[list[WorkflowNode]]:
    by_id: dict[str, WorkflowNode] = {}
    for n in nodes:
        if n.id in by_id:
            raise AgentError("WORKFLOW_DUPLICATE_ID", "Workflow node ids must be unique", retryable=False)
        by_id[n.id] = n
    for n in nodes:
        for dep in n.depends_on or []:
            if dep not in by_id:
                raise AgentError("WORKFLOW_UNKNOWN_DEP", f'Node "{n.id}" depends on unknown node "{dep}"', retryable=False)
            if dep == n.id:
                raise AgentError("WORKFLOW_CYCLE", f'Node "{n.id}" depends on itself', retryable=False)
    # Deduplicate first: what matters for the indegree is the number of
    # DISTINCT dependencies, not the raw list length.
    indegree = {n.id: len(set(n.depends_on or [])) for n in nodes}
    levels: list[list[WorkflowNode]] = []
    ready = [n for n in nodes if indegree[n.id] == 0]
    while ready:
        levels.append(ready)
        ready_ids = {n.id for n in ready}
        deps: dict[str, set[str]] = {m.id: set(m.depends_on or []) for m in nodes}
        nxt: list[WorkflowNode] = []
        for done in nodes:
            if done.id not in ready_ids:
                continue
            for m in nodes:
                if done.id not in deps[m.id]:
                    continue
                indegree[m.id] -= 1
                if indegree[m.id] == 0:
                    nxt.append(m)
        ready = nxt
    if sum(len(level) for level in levels) != len(nodes):
        raise AgentError("WORKFLOW_CYCLE", "Workflow has a dependency cycle", retryable=False)
    return levels


def _policy_of(node: WorkflowNode) -> RetryPolicy:
    retry = node.retry
    if isinstance(retry, dict):
        return RetryPolicy(**retry)
    if retry is None:
        return RetryPolicy()
    return retry


async def _run_with_retry(
    node: WorkflowNode,
    ctx: dict[str, Any],
    invocation_state: Optional[dict[str, Any]],
) -> Any:
    policy = _policy_of(node)
    attempts = max(1, policy.attempts or 1)
    backoff_ms = policy.backoff_ms or 0
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return await node.run(ctx, invocation_state)
        except asyncio.CancelledError:
            raise
        except AgentError as err:
            if err.code == "ABORTED":
                raise
            last = err
        except Exception as err:  # noqa: BLE001 - node body errors retry per policy
            last = err
        if i < attempts - 1 and backoff_ms > 0:
            await asyncio.sleep(backoff_ms / 1000)
    assert last is not None  # pragma: no cover - loop always sets it on fallthrough
    raise last


async def run_workflow(
    nodes: list[WorkflowNode],
    opts: Optional[WorkflowOptions] = None,
) -> dict[str, Any]:
    """Execute a DAG. Returns id -> result. Errors (incl. aborts) propagate."""
    invocation_state = opts.invocation_state if opts is not None else None
    ctx: dict[str, Any] = {}
    for level in _sort_topo(nodes):
        values = await asyncio.gather(*(_run_with_retry(n, ctx, invocation_state) for n in level))
        for node, value in zip(level, values):
            ctx[node.id] = value
    return ctx


__all__ = [
    "RetryPolicy",
    "WorkflowNode",
    "WorkflowOptions",
    "run_workflow",
]