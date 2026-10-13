"""Multi-agent orchestration. Parity: packages/typescript/src/orchestration/index.ts.

Strategies:
- "sequential": chained — each agent gets the previous agent's text appended
  to the original input; combined text is the LAST result.
- "parallel": fan-out with a concurrency cap; results keep agent order.
- "router": one model call picks an agent by index; only that agent runs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .agent import Agent, AgentResult
from .errors import AgentError
from .models import CompleteOptions, ModelProvider
from .types import ChatMessage

OrchestrationStrategy = Literal["sequential", "parallel", "router"]

ROUTER_SYSTEM = "You route user requests to specialist agents. Reply with ONLY the agent number."


@dataclass
class OrchestrationPlan:
    strategy: OrchestrationStrategy
    agents: list[Agent] = field(default_factory=list)
    """Max concurrent agents for parallel. Default: all at once."""
    concurrency: Optional[int] = None
    """Model that picks the agent for router. Defaults to the first agent's model."""
    router_model: Optional[ModelProvider] = None


@dataclass
class OrchestratedResult:
    strategy: OrchestrationStrategy
    results: list[AgentResult]
    """Combined text: last result (sequential/router), joined (parallel)."""
    text: str
    """Router only: absolute index of the chosen agent."""
    routed_to: Optional[int] = None


def _require_agents(plan: OrchestrationPlan) -> None:
    if not plan.agents:
        raise AgentError("ORCHESTRATION_EMPTY", "Orchestration needs at least one agent", retryable=False)


def _description_of(agent: Any) -> str:
    opts = getattr(agent, "options", None)
    desc = getattr(opts, "description", "") if opts is not None else ""
    if desc:
        return str(desc)
    fallback = getattr(agent, "description", "")
    return str(fallback) if fallback else ""


def _model_of(agent: Any) -> Optional[ModelProvider]:
    opts = getattr(agent, "options", None)
    model = getattr(opts, "model", None) if opts is not None else None
    if model is None:
        model = getattr(agent, "model", None)
    return model


async def _call_run(agent: Any, text: str, invocation_state: Optional[dict[str, Any]]) -> Any:
    """Agent.run takes no invocation-state knob; pass it when the callee does.

    Members of the plan may be plain Agent instances (run(text)) or richer
    runtimes that accept an invocation-state keyword.
    """
    if invocation_state is not None:
        try:
            sig = inspect.signature(agent.run)
        except (TypeError, ValueError):
            sig = None
        if sig is not None and any(
            p.kind is inspect.Parameter.VAR_KEYWORD or p.name == "invocation_state"
            for p in sig.parameters.values()
        ):
            return await agent.run(text, invocation_state=invocation_state)
    return await agent.run(text)


async def _run_sequential(
    plan: OrchestrationPlan, input_text: str, invocation_state: Optional[dict[str, Any]]
) -> OrchestratedResult:
    """Chain: each agent receives the previous agent's text appended to the input."""
    _require_agents(plan)
    results: list[AgentResult] = []
    current = input_text
    for i, agent in enumerate(plan.agents):
        res = await _call_run(agent, current, invocation_state)
        results.append(res)
        current = f"{input_text}\n\nPrevious result ({agent.name}):\n{res.text}"
    return OrchestratedResult("sequential", results, results[-1].text if results else "")


async def _run_parallel(
    plan: OrchestrationPlan, input_text: str, invocation_state: Optional[dict[str, Any]]
) -> OrchestratedResult:
    """Fan-out with a concurrency cap; results keep agent order."""
    _require_agents(plan)
    count = len(plan.agents)
    limit = plan.concurrency if plan.concurrency is not None else count
    semaphore = asyncio.Semaphore(max(1, min(max(1, limit), count)))
    results: list[AgentResult] = [None] * count  # type: ignore[list-item]

    async def run_one(i: int, agent: Any) -> None:
        async with semaphore:
            results[i] = await _call_run(agent, input_text, invocation_state)

    await asyncio.gather(*(run_one(i, a) for i, a in enumerate(plan.agents)))
    return OrchestratedResult("parallel", results, "\n\n".join(r.text for r in results))


async def _run_router(
    plan: OrchestrationPlan, input_text: str, invocation_state: Optional[dict[str, Any]]
) -> OrchestratedResult:
    """One model call picks the agent by index; only that agent runs."""
    _require_agents(plan)
    router_model = plan.router_model or _model_of(plan.agents[0])
    if router_model is None:
        raise AgentError("ROUTER_NO_MATCH", "Router strategy needs a model", retryable=False)
    menu = "\n".join(f"{i}: {a.name}" + (f" — {_description_of(a)}" if _description_of(a) else "")
                     for i, a in enumerate(plan.agents))
    pick = await router_model.complete(
        CompleteOptions(
            messages=[
                ChatMessage(role="system", content=ROUTER_SYSTEM),
                ChatMessage(role="user", content=f"Agents:\n{menu}\n\nRequest: {input_text}"),
            ],
            tools=[],
        )
    )
    match = re.search(r"\d+", pick.text or "")
    idx = int(match.group(0)) if match else -1
    if not (0 <= idx < len(plan.agents)):
        raise AgentError("ROUTER_NO_MATCH", f"Router picked invalid agent: {json.dumps(pick.text)}", retryable=False)
    res = await _call_run(plan.agents[idx], input_text, invocation_state)
    return OrchestratedResult("router", [res], getattr(res, "text", ""), routed_to=idx)


async def run_orchestrated(
    plan: OrchestrationPlan,
    input_text: str,
    invocation_state: Optional[dict[str, Any]] = None,
) -> OrchestratedResult:
    """Run a multi-agent plan. invocation_state threads to agents that accept it."""
    if plan.strategy == "sequential":
        return await _run_sequential(plan, input_text, invocation_state)
    if plan.strategy == "parallel":
        return await _run_parallel(plan, input_text, invocation_state)
    if plan.strategy == "router":
        return await _run_router(plan, input_text, invocation_state)
    raise AgentError("ORCHESTRATION_STRATEGY", f"Unknown strategy: {plan.strategy}", retryable=False)


__all__ = [
    "OrchestrationPlan",
    "OrchestrationStrategy",
    "OrchestratedResult",
    "run_orchestrated",
]