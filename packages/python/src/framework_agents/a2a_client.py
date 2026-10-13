"""A2A wire client. Parity: src/client/index.ts + src/a2a/index.ts (fixed).

Pinned contract (the TS client once POSTed without the JSON-RPC envelope and
the server ran every stream on an EMPTY message):

- AgentClient.stream POSTs `{jsonrpc, id, method: "message/stream", params:
  {message: {parts}, contextId?}}` to `${endpoint}/stream`, Accept SSE.
- SSE frames: `{"contextId", "event"}` / `{"contextId", "done", "text"}` /
  `{"error"}`; `[DONE]` terminates. on_context receives the server-assigned
  id so multi-turn continuation works.
- A2AClient.send POSTs `message/send` to the endpoint root and reads the
  server-assigned contextId from RESULT (never made up by the caller).
- AgentClientOptions.timeout_ms (default 300s) governs every send/get_card/
  stream (httpx.Timeout, TS AbortSignal.timeout parity — httpx's 5s default
  never applies); AgentClient.stream's `signal` aborts mid-stream (the
  streaming response is closed and the generator returns).
- Network/5xx retry with Retry-After honored (public endpoints rate-limit).
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional

import httpx

from .errors import AgentError

_sseq = itertools.count(1)


@dataclass
class AgentClientOptions:
    fetch_fn: Callable[[], httpx.AsyncClient] | None = None  # injectable for tests
    timeout_ms: float = 300_000.0


def _client_with_timeout(client: Any, opts: AgentClientOptions) -> Any:
    """Apply ``opts.timeout_ms`` to the whole exchange (TS AbortSignal.timeout
    parity) — without this httpx's own 5s default governs every send/get_card/
    stream, silently ignoring the 300s the options promise."""
    try:
        client.timeout = httpx.Timeout(opts.timeout_ms / 1000.0)
    except (AttributeError, TypeError, ValueError):
        pass  # injected test double without a settable timeout: leave as-is
    return client


def _signal_fired(signal: Any) -> bool:
    """Sync "is it aborted?" probe (asyncio.Event / Future / Task, or any
    object exposing ``is_set()`` / ``done()``)."""
    check = getattr(signal, "is_set", None)
    if callable(check):
        return bool(check())
    done = getattr(signal, "done", None)
    if callable(done):
        return bool(done())
    return False


async def _await_signal(signal: Any) -> None:
    """Block until the caller's abort signal fires: asyncio.Event / Future /
    Task, a callable returning an awaitable, or (fallback) any flag that
    ``_signal_fired`` understands, polled off the event loop."""
    if isinstance(signal, asyncio.Event):
        await signal.wait()
    elif isinstance(signal, (asyncio.Future, asyncio.Task)):
        await signal
    elif callable(signal):
        value = signal()
        if inspect.isawaitable(value):
            await value
    else:
        while not _signal_fired(signal):
            await asyncio.sleep(0.02)


@dataclass
class _RetryPolicy:
    max_retries: int = 2


class _HttpRetry:
    """429/5xx/network retry honoring Retry-After (shared by both clients)."""

    def __init__(self, policy: _RetryPolicy | None = None) -> None:
        self.policy = policy or _RetryPolicy()

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        last: AgentError | None = None
        for attempt in range(self.policy.max_retries + 1):
            if attempt > 0:
                delay = self._delay_ms or min(2000 * 2 ** (attempt - 1), 10_000)
                import asyncio

                await asyncio.sleep(delay / 1000)
            try:
                # Streaming: build the request, send without consuming the
                # body (aiter_text works); retries only fire on status codes,
                # which are available before any body bytes are pulled.
                if stream:
                    built = client.build_request(method, url, **kwargs)
                    res = await client.send(built, stream=True)
                else:
                    res = await client.request(method, url, **kwargs)
            except Exception as err:
                last = AgentError("A2A_TRANSPORT", f"Request failed: {err}", retryable=True)
                self._delay_ms = None
                continue
            if res.status_code == 429 or res.status_code >= 500:
                header = res.headers.get("retry-after", "")
                try:
                    self._delay_ms = min(float(header) * 1000, 60_000)
                except ValueError:
                    self._delay_ms = None
                if stream:
                    # A streamed failed attempt holds its connection until
                    # closed; without this aclose() every retry would leak a
                    # pool slot (the response body was never pulled). Its
                    # body is unread (and now closed) — the status line
                    # alone becomes the error (reading .text would raise).
                    try:
                        await res.aclose()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
                    last = AgentError("A2A_TRANSPORT", f"HTTP {res.status_code}", retryable=True)
                else:
                    last = AgentError("A2A_TRANSPORT", f"HTTP {res.status_code}: {res.text[:300]}", retryable=True)
                continue
            return res
        raise last or AgentError("A2A_TRANSPORT", "request failed", retryable=True)

    _delay_ms: float | None = None


class AgentClient:
    """SSE streaming client against a Framework agent server (`/stream`)."""

    def __init__(self, endpoint: str, opts: AgentClientOptions | None = None) -> None:
        self.opts = opts or AgentClientOptions()
        self.endpoint = endpoint.rstrip("/")

    async def stream(
        self,
        text: str,
        context_id: Optional[str] = None,
        signal: Optional[Any] = None,
        on_context: Optional[Callable[[str], None]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield SSE event frames; ``signal`` (e.g. an asyncio.Event) ABORTS
        the pull mid-stream: the streaming response is closed, upstream ends,
        and the generator returns cleanly."""
        retry = _HttpRetry()
        factory = self.opts.fetch_fn or httpx.AsyncClient
        client = _client_with_timeout(factory(), self.opts)
        try:
            body = {
                "jsonrpc": "2.0",
                "id": f"a2a-{next(_sseq)}",
                "method": "message/stream",
                "params": {
                    "message": {"role": "user", "parts": [{"kind": "text", "text": text}]},
                    **({"contextId": context_id} if context_id else {}),
                },
            }
            res = await retry.request(
                client,
                "POST",
                f"{self.endpoint}/stream",
                stream=True,
                headers={"content-type": "application/json", "accept": "text/event-stream"},
                json=body,
            )
            if res.status_code >= 400:
                raise AgentError("A2A_TRANSPORT", f"Stream HTTP {res.status_code}", retryable=res.status_code >= 500)
            abort_state: dict[str, bool] = {"aborted": False}

            async def watchdog() -> None:
                try:
                    await _await_signal(signal)
                except asyncio.CancelledError:
                    return
                abort_state["aborted"] = True
                # Abort stops the pull: tearing down the streaming response
                # ends the exchange; upstream observes the closed connection.
                try:
                    await res.aclose()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass

            watcher: Optional["asyncio.Task[None]"] = None
            if signal is not None:
                watcher = asyncio.ensure_future(watchdog())

            def _aborted() -> bool:
                return abort_state["aborted"] or _signal_fired(signal)

            buffer = ""
            lines: list[str] = []
            try:
                async for chunk in res.aiter_text():
                    if watcher is not None and _aborted():
                        return
                    buffer += chunk
                    (lines, buffer) = (buffer.split("\n")[:-1], buffer.split("\n")[-1])
                    for line in lines:
                        stripped = line.strip()
                        if not stripped.startswith("data:"):
                            continue
                        payload = stripped[5:].strip()
                        # The server writes [DONE] BOTH ways on the wire (raw in
                        # TS tests, JSON-stringified by the TS server's writer).
                        if payload in ("[DONE]", "\"[DONE]\""):
                            return
                        try:
                            frame = json.loads(payload)
                        except ValueError:
                            continue
                        if not isinstance(frame, dict):
                            continue  # tolerate str/number frames
                        if frame.get("contextId") and on_context:
                            on_context(frame["contextId"])
                        if frame.get("error"):
                            raise AgentError("A2A_ERROR", str(frame["error"]), retryable=False)
                        event = frame.get("event")
                        if isinstance(event, dict) and isinstance(event.get("type"), str):
                            yield event
                            # The signal may fire while we are suspended at the
                            # yield; the next pull must abort, not continue.
                            if watcher is not None and _aborted():
                                return
            except httpx.TimeoutException as err:
                raise AgentError("A2A_TRANSPORT", f"Stream timed out: {err}", retryable=True) from err
            except (httpx.HTTPError, RuntimeError):
                if abort_state["aborted"]:
                    return  # the watchdog closed the stream: a clean abort
                raise
            finally:
                if watcher is not None:
                    watcher.cancel()
                    try:
                        await watcher
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # noqa: BLE001 - watchdog cleanup is best-effort
                        pass
        finally:
            if self.opts.fetch_fn is None:
                await client.aclose()
            elif hasattr(client, "aclose"):
                await client.aclose()


class A2AClient:
    """JSON-RPC message/send client (cards + non-streaming sends)."""

    def __init__(self, endpoint: str, opts: AgentClientOptions | None = None) -> None:
        self.opts = opts or AgentClientOptions()
        self.endpoint = endpoint.rstrip("/")

    async def get_card(self) -> dict[str, Any]:
        retry = _HttpRetry()
        client = _client_with_timeout((self.opts.fetch_fn or httpx.AsyncClient)(), self.opts)
        try:
            res = await retry.request(client, "GET", f"{self.endpoint}/.well-known/agent-card.json")
            if res.status_code >= 400:
                raise AgentError("A2A_CARD", f"Agent card fetch failed: HTTP {res.status_code}", retryable=res.status_code >= 500)
            card = res.json()
            if not card.get("name"):
                raise AgentError("A2A_CARD", "Invalid agent card (missing name)", retryable=False)
            return card
        finally:
            if hasattr(client, "aclose"):
                await client.aclose()

    async def send(self, text: str, context_id: Optional[str] = None) -> dict[str, Any]:
        retry = _HttpRetry()
        client = _client_with_timeout((self.opts.fetch_fn or httpx.AsyncClient)(), self.opts)
        try:
            body = {
                "jsonrpc": "2.0",
                "id": f"a2a-{next(_sseq)}",
                "method": "message/send",
                "params": {
                    "message": {"role": "user", "parts": [{"kind": "text", "text": text}]},
                    **({"contextId": context_id} if context_id else {}),
                },
            }
            res = await retry.request(
                client,
                "POST",
                self.endpoint,
                headers={"content-type": "application/json", "accept": "application/json"},
                json=body,
            )
            if res.status_code >= 400:
                raise AgentError("A2A_TRANSPORT", f"A2A HTTP {res.status_code}", retryable=res.status_code >= 500)
            rpc = res.json()
            if rpc.get("error"):
                raise AgentError("A2A_ERROR", str(rpc["error"].get("message", "error")), retryable=False)
            # Servers return the (possibly newly minted) contextId inside result.
            result = rpc.get("result") or {}
            return {
                "text": _text_of_parts(((result.get("message") or {}).get("parts")) or []),
                "contextId": result.get("contextId") or context_id,
            }
        finally:
            if hasattr(client, "aclose"):
                await client.aclose()


def _text_of_parts(parts: list[Any]) -> str:
    out = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "".join(out)


def a2a_as_tool(
    client: A2AClient,
    name: str = "remote_agent",
    description: Optional[str] = None,
) -> "Tool":
    """Wrap a remote agent as a framework Tool for orchestrators.

    Parity: TS a2aAsTool — execute returns the ``.text`` of ``client.send``;
    ``context_id`` (optional) keeps the remote conversation going.
    """
    from pydantic import create_model

    from .tools import define_tool

    _A2aInput = create_model(
        "A2aInput",
        input=(str, ...),
        context_id=(Optional[str], None),
    )

    async def execute(args: Any, ctx: Any = None) -> Any:
        result = await client.send(args.input, args.context_id)
        return result["text"]

    return define_tool(
        name=name,
        description=description or f"Remote A2A agent at {client.endpoint}",
        schema=_A2aInput,
        execute=execute,
    )