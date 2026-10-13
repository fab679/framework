"""A2A server: agent-card + JSON-RPC message/send + /stream SSE.

Parity: src/a2a/index.ts (the A2AServer half; the client lives in a2a_client).

Pinned contract (each was a real bug in TS once — the port encodes the FIXED
behavior):

- Message bodies are ALWAYS parsed as JSON-RPC 2.0
  `{jsonrpc, id, method, params: {message: {parts}, contextId}}`. The code
  TRUSTS the POST body (TS parity): a body that is not a JSON object surfaces
  as a JSON-RPC error, never a crash.
- A missing contextId is minted as `ctx-<timestamp36>`; the (possibly newly
  minted) contextId is echoed inside the RESULT so multi-turn continuation
  works.
- Agents are cached PER CONTEXT with LRU eviction (same contextId -> the SAME
  agent instance; never one instance shared across contexts).
- /stream frames are exactly `{contextId, event}` | `{contextId, done, text}`
  | `{contextId, error}` and terminate with a bare `[DONE]` frame.
- Client disconnects abort the run: a guard task watches the socket and
  cancels the pump (mirror of TS `res.on("close")` + AbortController); since
  the SSE headers are already out, the abort is reported as a final error
  frame at best effort.
- Errors before any headers: JSON-RPC `{jsonrpc, id: null, error: {code:
  -32603}}` — the pinned special case is an oversized body, answered 413.
  After headers are sent (mid-stream failure) the error goes onto the wire
  as an SSE error frame instead.
- HEAD on any route: headers only (Content-Length of the would-be body, no
  body bytes) — Node suppresses HEAD bodies automatically, this port too.
- When a checkpointer is configured, runs execute with
  `{checkpointer, threadId: contextId}` (durable HITL threads), passed only
  if the agent's run/stream accepts a second argument.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from http import HTTPStatus
from typing import Any, Awaitable, Callable, Optional, Protocol, Union, runtime_checkable

from .agent import Agent
from .errors import AgentError
from .types import ChatMessage

DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
_MAX_HEADER_BYTES = 64 * 1024

# Reason phrases are status-specific (a 500 must never read "OK"): pinned
# phrases first, then the HTTPStatus registry, then a sane fallback.
_PHRASE: dict[int, str] = {
    **{int(member): member.phrase for member in HTTPStatus},
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    413: "Payload Too Large",
}

AgentFactory = Callable[[str], Union[Agent, Awaitable[Agent]]]


@runtime_checkable
class Checkpointer(Protocol):
    """Durable threads: save/load/delete a context's message history."""

    async def save(self, thread_id: str, messages: list[ChatMessage]) -> None: ...

    async def load(self, thread_id: str) -> list[ChatMessage]: ...

    async def delete(self, thread_id: str) -> None: ...


def _mint_context_id() -> str:
    """`ctx-<timestamp36>` — TS parity (Date.now().toString(36))."""
    value, digits = int(time.time() * 1000), ""
    while value:
        value, rem = divmod(value, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[rem] + digits
    return f"ctx-{digits or '0'}"


def _text_of_parts(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "".join(out)


def _as_rpc_dict(raw: Any) -> dict[str, Any]:
    # The server TRUSTS the body (TS casts the parse result); a non-object
    # behaves like an RPC object with every field undefined.
    return raw if isinstance(raw, dict) else {}


def _message(err: BaseException) -> str:
    return str(err) or err.__class__.__name__


def _accepts_run_options(bound_method: Callable[..., Any]) -> bool:
    """True if run/stream accepts a second (run options) argument."""
    try:
        sig = inspect.signature(bound_method)
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    rest = list(sig.parameters.values())[1:]  # skip the input argument
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in rest):
        return True
    return any(
        p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD) for p in rest
    )


class A2AServer:
    """Serve a Framework agent over A2A (card + message/send + /stream)."""

    def __init__(
        self,
        card: dict[str, Any],
        agent_factory: AgentFactory,
        max_contexts: int = 1000,
        checkpointer: Optional[Checkpointer] = None,
        cors_origins: Optional[list[str]] = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self.card = card
        self.agent_factory = agent_factory
        self.max_contexts = max(1, max_contexts)
        self.checkpointer = checkpointer
        self.cors_origins = list(cors_origins or [])
        self.max_body_bytes = max_body_bytes
        self._server: Optional[asyncio.AbstractServer] = None
        # Plain dicts are insertion-ordered: this IS the LRU (reinsert on hit).
        self._contexts: dict[str, Agent] = {}

    # -- lifecycle ----------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Bind and return the real port (port 0 => ephemeral port)."""
        self._server = await asyncio.start_server(self._handle_conn, host=host, port=port, limit=_MAX_HEADER_BYTES)
        assert self._server.sockets is not None, "asyncio server always has a socket"
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        self._contexts.clear()

    # -- context management --------------------------------------------------

    async def _agent_for(self, context_id: str) -> Agent:
        """Same contextId -> the SAME agent (LRU-retained); never shared across contexts."""
        existing = self._contexts.get(context_id)
        if existing is not None:
            del self._contexts[context_id]  # move-to-end
            self._contexts[context_id] = existing
            return existing
        agent = self.agent_factory(context_id)
        if inspect.isawaitable(agent):
            # Known concurrency quirk, shared with the TS port: a COLD cache
            # can double-build — two concurrent requests for the same unseen
            # context both reach this await, each resolves the factory, and
            # the later insert overwrites the first agent instance (its
            # state/history is dropped). Documented, NOT changed to preserve
            # TS semantics; a per-context in-flight build map is future work.
            agent = await agent
        self._contexts[context_id] = agent
        while len(self._contexts) > self.max_contexts:
            oldest = next(iter(self._contexts))
            del self._contexts[oldest]
        return agent

    def _run_opts(self, context_id: str) -> dict[str, Any]:
        if self.checkpointer is None:
            return {}
        return {"checkpointer": self.checkpointer, "threadId": context_id}

    async def _run_agent(self, agent: Agent, context_id: str, text: str) -> Any:
        opts = self._run_opts(context_id)
        if opts and _accepts_run_options(agent.run):
            return await agent.run(text, opts)
        return await agent.run(text)

    # -- HTTP plumbing ---------------------------------------------------------

    def _cors_headers(self, origin: Optional[str]) -> dict[str, str]:
        allowed = self.cors_origins
        if not allowed or not origin:
            return {}
        if "*" not in allowed and origin not in allowed:
            return {}
        return {
            "access-control-allow-origin": "*" if "*" in allowed else origin,
            "access-control-allow-methods": "GET, POST, OPTIONS",
            "access-control-allow-headers": "content-type, authorization",
        }

    async def _read_body(self, reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
        """Read the request body with a hard cap — no more than `max_body_bytes` is ever buffered."""
        max_len = self.max_body_bytes
        if "chunked" in headers.get("transfer-encoding", "").lower():
            chunks: list[bytes] = []
            total = 0
            while True:
                size_line = await reader.readline()
                size = int(size_line.strip().split(b";")[0], 16)
                if size == 0:
                    while True:  # trailers end at a blank line (ignored)
                        line = await reader.readline()
                        if not line or line in (b"\r\n", b"\n"):
                            break
                    break
                total += size
                if total > max_len:
                    raise AgentError("A2A_BODY_TOO_LARGE", f"Request body exceeds {max_len} bytes", retryable=False)
                chunks.append(await reader.readexactly(size))
                await reader.readexactly(2)  # chunk CRLF
            return b"".join(chunks)
        raw_length = headers.get("content-length")
        length = int(raw_length) if raw_length is not None else 0
        if length > max_len:  # rejected before ANY byte of the body is read
            raise AgentError("A2A_BODY_TOO_LARGE", f"Request body exceeds {max_len} bytes", retryable=False)
        if length == 0:
            return b""
        return await reader.readexactly(length)

    def _send(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        cors: dict[str, str],
        extra_headers: Optional[dict[str, str]] = None,
        content_type: str = "application/json",
        with_content_length: bool = True,
        send_body: bool = True,
    ) -> None:
        """Write a response. ``send_body=False`` (HEAD): headers only — the
        Content-Length is still the WOULD-BE body's, but no body bytes go out
        (Node suppresses HEAD bodies automatically; this port must do it)."""
        headers = {"connection": "close", **cors, **(extra_headers or {})}
        if with_content_length:
            headers["content-length"] = str(len(body))
        headers["content-type"] = content_type
        head = f"HTTP/1.1 {status} {_PHRASE.get(status, 'Error' if status >= 400 else 'OK')}\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in headers.items()
        )
        writer.write(head.encode("utf-8") + b"\r\n" + (body if send_body else b""))

    def _send_json(self, writer: asyncio.StreamWriter, status: int, payload: Any, cors: dict[str, str]) -> None:
        self._send(writer, status, json.dumps(payload).encode("utf-8"), cors)

    def _send_head(self, writer: asyncio.StreamWriter, status: int, payload: Any, cors: dict[str, str]) -> None:
        """HEAD response: the headers GET would have sent, body suppressed."""
        self._send(writer, status, json.dumps(payload).encode("utf-8"), cors, send_body=False)

    @staticmethod
    def _frame(writer: asyncio.StreamWriter, payload: Any) -> None:
        writer.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))

    @staticmethod
    async def _frame_ok(writer: asyncio.StreamWriter, payload: Any) -> None:
        """One SSE data frame (drains the socket)."""
        A2AServer._frame(writer, payload)
        await writer.drain()

    async def _watch_disconnect(self, reader: asyncio.StreamReader, pump: "asyncio.Task[None]", finished: asyncio.Event) -> None:
        """Mirror of TS `res.on("close")` + AbortController: the peer going
        away cancels the pump (a disconnect ABORTS the run, never finishes it)."""
        try:
            while True:
                data = await reader.read(4096)  # extra bytes are stray; we wait for EOF
                if not data:
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass
        if not finished.is_set():
            pump.cancel()

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle_one(reader, writer)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, asyncio.IncompleteReadError, OSError):
            pass  # socket died mid-response — the route handlers already did what they could
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):  # pragma: no cover - torn-down peer
                pass

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str]]:
        """(method, path, headers) — header names are lowercased."""
        request_line = await reader.readline()
        parts = request_line.split() if request_line else []
        if len(parts) < 2:
            raise asyncio.IncompleteReadError(request_line, None)
        method = parts[0].decode("latin-1")
        path = parts[1].decode("latin-1").split("?")[0]
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            key, _, value = line.partition(b":")
            headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
        return method, path, headers

    async def _handle_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        method, path, headers = await self._read_request(reader)
        cors = self._cors_headers(headers.get("origin"))
        # Mutated by _handle_stream the moment the SSE headers are out —
        # BEFORE that, every failure is answered as a normal HTTP response.
        sse: dict[str, bool] = {"started": False}

        try:
            if method == "OPTIONS":
                self._send(writer, 204, b"", cors)
                await writer.drain()
                return
            if method == "HEAD":
                # HEAD mirrors GET semantics (headers only; POST-only routes
                # are not GET-routable). Node suppresses HEAD bodies itself —
                # this port sends the Content-Length of the would-be body but
                # never any body bytes.
                if path in ("/ok", "/health"):
                    self._send_head(writer, 200, {"ok": True}, cors)
                elif path == "/.well-known/agent-card.json":
                    self._send_head(writer, 200, self.card, cors)
                else:
                    self._send_head(writer, 404, {"error": "not found"}, cors)
                await writer.drain()
                return
            if method == "GET" and path in ("/ok", "/health"):
                self._send_json(writer, 200, {"ok": True}, cors)
                await writer.drain()
                return
            if method == "GET" and path == "/.well-known/agent-card.json":
                self._send_json(writer, 200, self.card, cors)
                await writer.drain()
                return
            if method == "POST" and path in ("/", ""):
                await self._handle_send(reader, writer, headers, cors)
                return
            if method == "POST" and path == "/stream":
                await self._handle_stream(reader, writer, headers, cors, sse)
                return
            self._send_json(writer, 404, {"error": "not found"}, cors)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            return  # peer vanished mid-write; nothing more to send
        except Exception as err:  # noqa: BLE001 — every failure maps onto the wire
            message = _message(err)
            if sse["started"]:
                # Mid-stream failure (TS "headersSent" branch): SSE headers are
                # already out, so report the error as SSE frames.
                try:
                    writer.write(f"data: {json.dumps({'error': message})}\n\n".encode() + b"data: [DONE]\n\n")
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    pass  # socket gone — nothing more to do
                return
            # Pre-header failure (body oversize / malformed JSON-RPC): a real
            # HTTP response with a proper status line — never SSE frames.
            status = 413 if isinstance(err, AgentError) and err.code == "A2A_BODY_TOO_LARGE" else 200
            self._send_json(writer, status, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": message}}, cors)
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):  # pragma: no cover
                pass

    async def _handle_send(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        cors: dict[str, str],
    ) -> None:
        # Trust-the-body parity: JSON parse errors bubble to the caller (-32603).
        rpc = _as_rpc_dict(json.loads(await self._read_body(reader, headers)))
        params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}
        if rpc.get("method") != "message/send":
            self._send_json(
                writer,
                200,
                {"jsonrpc": "2.0", "id": rpc.get("id"), "error": {"code": -32601, "message": f"Unknown method: {rpc.get('method')}"}},
                cors,
            )
            await writer.drain()
            return
        context_id = params.get("contextId") or _mint_context_id()
        text = _text_of_parts((params.get("message") or {}).get("parts") if isinstance(params.get("message"), dict) else None)
        agent = await self._agent_for(context_id)
        result = await self._run_agent(agent, context_id, text)
        self._send_json(
            writer,
            200,
            {
                "jsonrpc": "2.0",
                "id": rpc.get("id"),
                "result": {
                    "message": {"role": "agent", "parts": [{"kind": "text", "text": result.text}]},
                    "contextId": context_id,
                },
            },
            cors,
        )
        await writer.drain()

    async def _handle_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        cors: dict[str, str],
        sse: dict[str, bool],
    ) -> None:
        # Read + parse + validate the body BEFORE any SSE headers go out (TS
        # readBody-before-writeHead): a failure here — oversized body (413),
        # malformed JSON (JSON-RPC -32603) — is answered over HTTP with a real
        # status line. Only after this point are errors SSE error frames (the
        # headers are already on the wire, as in TS's "headersSent" branch).
        rpc = _as_rpc_dict(json.loads(await self._read_body(reader, headers)))
        params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}
        context_id = params.get("contextId") or _mint_context_id()
        text = _text_of_parts((params.get("message") or {}).get("parts") if isinstance(params.get("message"), dict) else None)
        # SSE headers go out BEFORE the agent is resolved (TS parity): a
        # failure from here on can only be an SSE error frame.
        sse["started"] = True
        self._send(
            writer,
            200,
            b"",
            cors,
            extra_headers={"cache-control": "no-cache", "connection": "keep-alive"},
            content_type="text/event-stream",
            with_content_length=False,
        )
        await writer.drain()

        finished = asyncio.Event()

        async def pump() -> None:
            try:
                agent = await self._agent_for(context_id)
                opts = self._run_opts(context_id)
                events = agent.stream(text, opts) if opts and _accepts_run_options(agent.stream) else agent.stream(text)
                async for event in events:
                    # Every yielded event is forwarded as an {event} frame —
                    # including the agent's own done event (which carries the
                    # final text), EXACTLY like the TS server. The closing
                    # {done: true, text} frame then terminates the SSE body
                    # (the TS wire has both frames; clients may read either).
                    await self._frame_ok(writer, {"contextId": context_id, "event": event})
                    if isinstance(event, dict) and event.get("type") == "done":
                        await self._frame_ok(writer, {"contextId": context_id, "done": True, "text": str(event.get("text") or "")})
            except asyncio.CancelledError:
                # Guard-cancelled: the client went away mid-run. The socket is
                # already gone, so the abort is reported at best effort.
                try:
                    self._frame(writer, {"contextId": context_id, "error": "Run aborted (client disconnected)"})
                except (ConnectionResetError, BrokenPipeError, OSError):
                    pass
                raise
            except Exception as err:  # noqa: BLE001 — SSE error-frame parity
                await self._frame_ok(writer, {"contextId": context_id, "error": _message(err)})

        pump_task = asyncio.ensure_future(pump())
        guard_task = asyncio.ensure_future(self._watch_disconnect(reader, pump_task, finished))
        cancelled = False
        try:
            await pump_task
        except asyncio.CancelledError:
            cancelled = True  # peer already gone — no frames would arrive
        finally:
            finished.set()
            guard_task.cancel()
            try:
                await guard_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if not cancelled:
            self._frame(writer, "[DONE]")
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass