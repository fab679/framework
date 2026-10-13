"""Swarm: autonomous handoffs across specialists. Parity: packages/typescript/src/swarm/index.ts.

Each member gets a `handoff_to_agent` tool; shared context (task, history,
contributed knowledge, roster) is rebuilt every turn. Safety: max handoffs /
iterations / timeouts + ping-pong detection. Setting a detection window alone
enables detection (min_unique_agents defaults to 2 when window > 0).
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Literal, Optional

from pydantic import BaseModel

from .agent import Agent, AgentResult
from .errors import AgentError
from .state import AgentState
from .tools import ToolContext, define_tool
from .types import TokenUsage

HANDOFF_TOOL = "handoff_to_agent"

SwarmStatus = Literal["COMPLETED", "FAILED"]


class HandoffArgs(BaseModel):
    to: str
    message: str
    context: Optional[dict[str, Any]] = None


@dataclass
class HandoffSignal:
    to: str
    message: str
    context: Optional[dict[str, Any]] = None


def handoff_signal(to: str, message: str, context: Optional[dict[str, Any]] = None) -> AgentError:
    err = AgentError("SWARM_HANDOFF", f"Handoff to {to}", retryable=False)
    err.handoff = HandoffSignal(to=to, message=message, context=context)  # type: ignore[attr-defined]
    return err


def read_handoff_signal(err: BaseException) -> Optional[HandoffSignal]:
    """The SWARM_HANDOFF AgentError carries the routing payload on `.handoff`."""
    if isinstance(err, AgentError) and err.code == "SWARM_HANDOFF":
        return getattr(err, "handoff", None)
    return None


@dataclass
class SwarmResult:
    status: SwarmStatus
    text: str
    node_history: list[str]
    results: dict[str, AgentResult]
    iterations: int
    handoffs: int
    usage: TokenUsage
    shared_context: dict[str, dict[str, Any]]
    error: Optional[str] = None


SwarmStreamEvent = dict[str, Any]


@dataclass
class Swarm:
    agents: list[Agent]
    entry_point: Optional[Any] = None  # Agent instance or name; default agents[0]
    max_handoffs: int = 20
    max_iterations: int = 20
    execution_timeout_ms: Optional[float] = None
    node_timeout_ms: Optional[float] = None
    loop_detection_window: int = 0
    min_unique_agents: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.agents:
            raise AgentError("SWARM_EMPTY", "Swarm needs at least one agent", retryable=False)
        if isinstance(self.entry_point, str):
            match = next((a for a in self.agents if a.name == self.entry_point), None)
            if match is None:
                raise AgentError("SWARM_UNKNOWN_ENTRY", f"Unknown entry point: {self.entry_point}", retryable=False)
            self.entry_point = match
        elif self.entry_point is None:
            self.entry_point = self.agents[0]
        # A window alone must enable detection — the uniqueness threshold
        # defaults instead of requiring both knobs (old behavior: window > 0
        # with min_unique_agents unset silently detected nothing).
        self._min_unique = self.min_unique_agents if self.min_unique_agents is not None else (2 if self.loop_detection_window > 0 else 0)

    # ---- helpers -------------------------------------------------------

    def _roster(self, except_name: str) -> str:
        return "\n".join(
            f"- {a.name}" + (f": {a.options.description}" if a.options.description else "")
            for a in self.agents
            if a.name != except_name
        )

    def _build_input(self, task: str, history: list[str], shared: dict[str, dict[str, Any]],
                     message: str, current: str) -> str:
        knowledge = "\n".join(f"• {agent}: {json.dumps(facts)}" for agent, facts in shared.items())
        parts = [
            f"Original task: {task}",
            f"Handoff message: {message}" if message else "",
            f"Agents so far: {' → '.join(history)}" if history else "",
            f"Shared knowledge:\n{knowledge}" if knowledge else "",
            f"Available collaborators:\n{self._roster(current)}",
            f"If you need a collaborator, call {HANDOFF_TOOL} with their name. Otherwise finish with your final answer as text.",
        ]
        return "\n\n".join(p for p in parts if p)

    def _member_with_handoff(self, agent: Agent, shared: dict[str, dict[str, Any]]) -> Agent:
        """Clone keeps the member's model/tools; handoff coordination is added.

        Memory/sessions/state are intentionally fresh per handoff turn (shared
        knowledge flows through the swarm input instead): a retained
        session_manager would cross-pollinate per-member histories across
        handoffs, and a SHARED AgentState would write a member's state back
        into the original agent — so the manager is dropped and state is
        copied via to_json/from_json, never aliased.
        """
        member = agent

        async def execute(args: Any, ctx: ToolContext | None = None) -> Any:
            if not any(a.name == args.to for a in self.agents):
                names = ", ".join(a.name for a in self.agents)
                return f'Error: unknown agent "{args.to}". Available: {names}. Pick one or finish yourself.'
            if args.context:
                shared[member.name] = {**(shared.get(member.name) or {}), **args.context}
            raise handoff_signal(args.to, args.message, args.context)

        handoff = define_tool(
            name=HANDOFF_TOOL,
            description=(
                "Hand control to a specialist. Args: to (agent name), message "
                "(what you need), context (facts to share as an object)."
            ),
            schema=HandoffArgs,
            execute=execute,
        )
        opts = replace(
            agent.options,
            tools=[*agent.options.tools, handoff],
            memory=None,
            session_manager=None,
            state=AgentState.from_json(agent.state.to_json()),
        )
        return Agent(opts)

    def _detect_loop(self, trail: list[str]) -> Optional[str]:
        window_size = self.loop_detection_window
        if window_size <= 0 or self._min_unique <= 0:
            return None
        window = trail[-window_size:] if window_size > 0 else trail
        if len(window) < window_size:
            return None
        if len(set(window)) < self._min_unique:
            return (
                f"Repetitive handoff detected ({' → '.join(window)}): fewer than "
                f"{self._min_unique} unique agents in the last {window_size} trail entries"
            )
        return None

    def _finish(self, status: SwarmStatus, error: str, history: list[str],
                results: dict[str, AgentResult], usage: TokenUsage,
                shared: dict[str, dict[str, Any]], iterations: int, handoffs: int,
                text: str = "") -> SwarmResult:
        return SwarmResult(
            status=status,
            text=text,
            node_history=[*history],
            results=results,
            iterations=iterations,
            handoffs=handoffs,
            usage=usage,
            shared_context=shared,
            error=error or None,
        )

    # ---- run ------------------------------------------------------------

    async def run(self, task: str) -> SwarmResult:
        """Drain the stream (TS run() drains stream())."""
        result: Optional[SwarmResult] = None
        async for event in self.stream(task):
            if event.get("type") == "result":
                result = event["result"]
        if result is None:  # pragma: no cover - stream always yields a result
            raise AgentError("SWARM_INCOMPLETE", "Swarm stream drained without a result", retryable=False)
        return result

    async def stream(self, task: str) -> AsyncIterator[SwarmStreamEvent]:
        """Yield node_start/node_done/handoff/agent_event, then the result.

        Python async generators cannot carry return values (unlike TS), so the
        final SwarmResult also arrives as a {"type": "result", ...} event —
        including failures.
        """
        shared: dict[str, dict[str, Any]] = {}
        history: list[str] = []
        results: dict[str, AgentResult] = {}
        usage = TokenUsage()
        trail: list[str] = []
        current: Any = self.entry_point
        message = ""
        iterations = 0
        handoffs = 0

        def fail(error: str) -> SwarmResult:
            return self._finish("FAILED", error, history, results, usage, shared, iterations, handoffs)

        exec_cm = (
            asyncio.timeout(self.execution_timeout_ms / 1000)
            if self.execution_timeout_ms is not None
            else nullcontext()
        )
        exec_deadline = (
            time.monotonic() + self.execution_timeout_ms / 1000
            if self.execution_timeout_ms is not None
            else None
        )
        try:
            async with exec_cm:
                while True:
                    iterations += 1
                    if iterations > self.max_iterations:
                        res = fail(f"Exceeded maxIterations ({self.max_iterations})")
                        yield {"type": "result", "result": res}
                        return
                    yield {"type": "node_start", "agent": current.name}
                    runner = self._member_with_handoff(current, shared)
                    result: Optional[AgentResult] = None
                    node_cm = (
                        asyncio.timeout(self.node_timeout_ms / 1000)
                        if self.node_timeout_ms is not None
                        else nullcontext()
                    )
                    try:
                        async with node_cm:
                            agen = runner.stream(
                                self._build_input(task, history, shared, message, current.name)
                            )
                            try:
                                async for event in agen:
                                    yield {"type": "agent_event", "agent": current.name, "event": event}
                            finally:
                                await agen.aclose()
                            result = getattr(runner, "_last_result", None)
                    except TimeoutError:
                        if exec_deadline is not None and time.monotonic() >= exec_deadline:
                            raise  # execution-level timeout, not this node's
                        res = fail(f"Agent {current.name} timed out")
                        yield {"type": "result", "result": res}
                        return
                    except AgentError as err:
                        handoff = read_handoff_signal(err)
                        if handoff is None:
                            res = fail(f"Agent {current.name} failed: {err}")
                            yield {"type": "result", "result": res}
                            return
                        handoffs += 1
                        if handoffs > self.max_handoffs:
                            res = fail(f"Exceeded maxHandoffs ({self.max_handoffs})")
                            yield {"type": "result", "result": res}
                            return
                        trail.extend([current.name, handoff.to])
                        yield {"type": "handoff", "from": current.name, "to": handoff.to}
                        loop = self._detect_loop(trail)
                        if loop:
                            res = fail(loop)
                            yield {"type": "result", "result": res}
                            return
                        nxt = next((a for a in self.agents if a.name == handoff.to), None)
                        if nxt is None:  # pragma: no cover - tool filters unknown names
                            res = fail(f"Handoff to unknown agent: {handoff.to}")
                            yield {"type": "result", "result": res}
                            return
                        history.append(current.name)
                        message = handoff.message
                        current = nxt
                        continue
                    # Completed without handoff -> final answer.
                    if result is None:
                        res = fail(f"Agent {current.name} returned no result")
                        yield {"type": "result", "result": res}
                        return
                    history.append(current.name)
                    results[current.name] = result
                    usage = usage + result.usage
                    yield {"type": "node_done", "agent": current.name}
                    done = self._finish("COMPLETED", "", history, results, usage, shared, iterations, handoffs, result.text)
                    yield {"type": "result", "result": done}
                    return
        except TimeoutError:
            res = fail(f"Exceeded executionTimeoutMs ({self.execution_timeout_ms})")
            yield {"type": "result", "result": res}
            return


__all__ = [
    "HandoffArgs",
    "HandoffSignal",
    "Swarm",
    "SwarmResult",
    "SwarmStreamEvent",
    "HANDOFF_TOOL",
    "handoff_signal",
    "read_handoff_signal",
]