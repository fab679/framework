"""MCP client: stdio + Streamable HTTP transports, and tool bridging.

Parity: src/mcp/index.ts.

Pinned behaviors (the TS fixes are baked in):

- initialize handshake sends clientInfo {"name": "framework-agents",
  "version": "0.1.0"}; `notifications/initialized` is a NOTIFICATION: sent
  without an id and never awaited on a response (stdio: fire-and-forget;
  HTTP: the reply body is read and DISCARDED — streamable servers answer
  notifications with 202/SSE and a client waiting on them would hang).
- stdio: line-delimited JSON-RPC with id correlation. The spawned process
  EXITING (crash, OOM, kill) rejects every pending request — without this
  'error' alone would never fire and in-flight calls would hang forever.
- close() settles in-flight requests with an error instead of dropping them.
- tools/call unpacks content text parts; isError=True raises MCP_TOOL_ERROR.
- mcp_tools_to_framework bridges JSON Schema -> pydantic dynamically. Enums
  become Literal (string) or Union[Literal, ...] (numbers/mixed). A tool
  whose schema cannot be bridged degrades to a permissive schema — one bad
  tool never crashes bridging for ALL of them.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Literal, Optional, Protocol, Type, Union

import httpx
from pydantic import BaseModel, ConfigDict, create_model

from .errors import AgentError
from .tools import Tool, define_tool

CLIENT_NAME = "framework-agents"
CLIENT_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

_rpc_seq = itertools.count(1)


class McpToolDef(dict):
    """Loose dict shape: {name, description?, inputSchema}."""


def _check_rpc(res: dict[str, Any], what: str) -> Any:
    error = res.get("error")
    if error:
        raise AgentError("MCP_ERROR", f"MCP {what}: {error.get('message', '')} ({error.get('code')})", retryable=False)
    return res.get("result")


def _text_of_content(result: dict[str, Any]) -> str:
    content = result.get("content") if isinstance(result, dict) else None
    parts = [c.get("text", "") or "" for c in content if isinstance(c, dict) and c.get("type") == "text"] if isinstance(content, list) else []
    return "\n".join(parts)


def _call_result_check(name: str, result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    text = _text_of_content(result)
    if result.get("isError"):
        raise AgentError("MCP_TOOL_ERROR", f'MCP tool "{name}" failed: {text}', retryable=False)
    return text


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------


class McpStdioClient:
    """Spawns `command args`, speaks line-delimited JSON-RPC on its stdio."""

    def __init__(
        self,
        command: str,
        args: Optional[list[str]] = None,
        *,
        client_name: str = CLIENT_NAME,
        server_id: Optional[str] = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.server_id = server_id or " ".join([command, *self.args]).strip()
        self.client_name = client_name
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._pending: dict[int, "asyncio.Future[dict[str, Any]]"] = {}
        self._reader_task: Optional["asyncio.Task[None]"] = None
        self._watcher_task: Optional["asyncio.Task[None]"] = None
        self._client_info = {"name": self.client_name, "version": CLIENT_VERSION}

    async def connect(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as err:
            raise AgentError("MCP_TRANSPORT", f"Cannot spawn MCP server: {err}", retryable=False) from err
        assert self.proc.stdout is not None and self.proc.stdin is not None
        self._reader_task = asyncio.ensure_future(self._read_loop())
        self._watcher_task = asyncio.ensure_future(self._watch_exit())
        await self._request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": self._client_info},
        )
        # Notification: no id, no response wait (any reply line has no id to match).
        self._send_line({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        try:
            await self._flush()  # best effort — a dead pipe must not fail connect()
        except AgentError:  # pragma: no cover - server died right after init
            pass

    def _send_line(self, message: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise AgentError("MCP_NOT_CONNECTED", "Call connect() first", retryable=False)
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")

    async def _flush(self) -> None:
        if self.proc is not None and self.proc.stdin is not None:
            try:
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as err:
                raise AgentError("MCP_TRANSPORT", f"MCP stdio write failed: {err}", retryable=True) from err

    def _reject_pending(self, message: str) -> None:
        """Settle in-flight requests — dropping them (TS once did) hangs callers."""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(AgentError("MCP_TRANSPORT", message, retryable=True))
        self._pending.clear()

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            assert self.proc is not None and self.proc.stdout is not None
            try:
                line = await self.proc.stdout.readline()
            except asyncio.CancelledError:
                return
            if not line:
                break  # EOF
            stripped = line.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except ValueError:
                continue
            mid = msg.get("id") if isinstance(msg, dict) else None
            fut = self._pending.get(mid)
            if fut is not None and not fut.done():
                loop.call_soon(fut.set_result, msg)
        # EOF (stdout closed, process dead or wedged): in-flight requests
        # would otherwise hang forever.
        self._reject_pending("MCP server closed its output stream")

    async def _watch_exit(self) -> None:
        # Spawn succeeded but the process later dies (crash, OOM, kill):
        # without this watcher in-flight requests hang forever.
        assert self.proc is not None
        code = await self.proc.wait()
        self._reject_pending(f"MCP server exited (code {code})")

    async def _request(self, method: str, params: Any) -> Any:
        if self.proc is None or self.proc.stdin is None:
            raise AgentError("MCP_NOT_CONNECTED", "Call connect() first", retryable=False)
        rid = next(_rpc_seq)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            self._send_line({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            await self._flush()
            reply = await fut
        finally:
            self._pending.pop(rid, None)
        return _check_rpc(reply, method)

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        return list(result.get("tools") or []) if isinstance(result, dict) else []

    async def call_tool(self, name: str, args: Any) -> str:
        result = await self._request("tools/call", {"name": name, "arguments": args if args is not None else {}})
        return _call_result_check(name, result)

    async def close(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - already dead
                pass
        # Settle in-flight requests BEFORE the exit watcher races us.
        self._reject_pending("MCP client closed")
        for task in (self._reader_task, self._watcher_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._reader_task = self._watcher_task = None
        if proc is not None:
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001 - process may be awaited already
                pass


# ---------------------------------------------------------------------------
# Streamable HTTP transport
# ---------------------------------------------------------------------------


async def _parse_streamable(res: httpx.Response) -> dict[str, Any]:
    """JSON or (streamable) SSE body -> the last response-shaped message."""
    content_type = res.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        lines = res.text.split("\n")
        last: Optional[dict[str, Any]] = None
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            payload = stripped[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict) and (msg.get("id") is not None or msg.get("result") is not None or msg.get("error")):
                last = msg
        if last is None:
            raise AgentError("MCP_PROTOCOL", "Empty SSE response from MCP server", retryable=True)
        return last
    body = res.json()
    return body if isinstance(body, dict) else {}


class McpHttpClient:
    """MCP client over Streamable HTTP (POST JSON-RPC; JSON or SSE responses)."""

    def __init__(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        *,
        timeout_ms: Optional[float] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        client_name: str = CLIENT_NAME,
        server_id: Optional[str] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.server_id = server_id or url
        self._extra_headers = dict(headers or {})
        self._owns_client = http_client is None
        # httpx's own default timeout unless the caller pins one (timeout_ms).
        timeout: Any = httpx.Timeout(timeout_ms / 1000) if timeout_ms is not None else None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)
        self._session_id: Optional[str] = None
        self._connected = False
        self.client_name = client_name

    async def _rpc_inner(self, method: str, params: Any, with_id: bool) -> Any:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params if params is not None else {},
        }
        if with_id:
            body["id"] = next(_rpc_seq)
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            **({"mcp-session-id": self._session_id} if self._session_id else {}),
            **self._extra_headers,
        }
        try:
            res = await self._client.post(self.url, headers=headers, json=body)
        except httpx.HTTPError as err:
            raise AgentError("MCP_TRANSPORT", f"MCP request failed: {err}", retryable=True) from err
        session = res.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        if res.status_code >= 400:
            raise AgentError("MCP_TRANSPORT", f"MCP HTTP {res.status_code}", retryable=res.status_code >= 500)
        if not with_id:
            # Notifications carry no id: no response will ever match. Read and
            # DISCARD whatever the server sends instead of hanging on it.
            try:
                await res.aread()
            except httpx.HTTPError:
                pass
            return None
        return _check_rpc(await _parse_streamable(res), method)

    async def _rpc(self, method: str, params: Any = None) -> Any:
        return await self._rpc_inner(method, params, True)

    async def _notify(self, method: str, params: Any = None) -> None:
        await self._rpc_inner(method, params, False)

    async def connect(self) -> dict[str, Any]:
        result = await self._rpc(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": self.client_name, "version": CLIENT_VERSION}},
        )
        try:
            await self._notify("notifications/initialized", {})
        except AgentError:
            pass  # a notification failure must not break a live session
        self._connected = True
        server_info = result.get("serverInfo") if isinstance(result, dict) else {}
        server_info = server_info if isinstance(server_info, dict) else {}
        return {"name": server_info.get("name") or self.server_id, "version": server_info.get("version")}

    def _require_connected(self) -> None:
        if not self._connected:
            raise AgentError("MCP_NOT_CONNECTED", "Call connect() first", retryable=False)

    async def list_tools(self) -> list[dict[str, Any]]:
        self._require_connected()
        result = await self._rpc("tools/list", {})
        return list(result.get("tools") or []) if isinstance(result, dict) else []

    async def call_tool(self, name: str, args: Any) -> str:
        self._require_connected()
        result = await self._rpc("tools/call", {"name": name, "arguments": args if args is not None else {}})
        return _call_result_check(name, result)

    async def close(self) -> None:
        self._connected = False
        self._session_id = None
        if self._owns_client:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# JSON Schema -> pydantic bridging
# ---------------------------------------------------------------------------


class PermissiveArgs(BaseModel):
    """Degrade schema for tools whose input schema cannot be bridged."""

    model_config = ConfigDict(extra="allow")


def _literal_type(value: Any) -> type:
    if isinstance(value, (int, str, bytes, bool)) or value is None:
        return Literal[value]  # type: ignore[index]
    raise TypeError(f"JSON Schema enum value not representable as a literal: {value!r}")


def _annotation_from_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return Any
    if isinstance(schema.get("enum"), list):
        values = schema["enum"]
        if not values:
            return Any
        literals = [_literal_type(v) for v in values]
        if len(literals) == 1:
            return literals[0]
        return Union[tuple(literals)]  # numeric/mixed enums: Union[Literal, ...]
    type_ = schema.get("type")
    if type_ == "string":
        return str
    if type_ == "number":
        return float
    if type_ == "integer":
        return int
    if type_ == "boolean":
        return bool
    if type_ == "null":
        return type(None)
    if type_ == "array":
        items = schema.get("items")
        return list[_annotation_from_schema(items) if isinstance(items, dict) else Any]  # type: ignore[misc]
    if type_ == "object":
        return json_schema_to_model(schema, name="Nested")
    return Any


def json_schema_to_model(schema: dict[str, Any], name: str = "McpArgs") -> Type[BaseModel]:
    """Minimal JSON Schema -> pydantic (objects, scalars, arrays; else Any)."""
    if not isinstance(schema, dict) or schema.get("type") not in ("object", None):
        raise TypeError(f"Unsupported tool input schema shape: {schema.get('type')!r}")
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for key, sub in props.items():
        annotation = _annotation_from_schema(sub if isinstance(sub, dict) else {})
        if key in required:
            fields[key] = (annotation, ...)
        else:
            fields[key] = (Optional[annotation], None)  # type: ignore[misc]
    # extra="allow" mirrors z.passthrough — unknown props keep flowing.
    model = create_model(name, __config__=ConfigDict(extra="allow"), **fields)
    return model


class McpClient(Protocol):
    server_id: str

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, args: Any) -> str: ...

    async def close(self) -> None: ...


async def mcp_tools_to_framework(
    client: McpClient,
    defs: Optional[list[dict[str, Any]]] = None,
) -> list[Tool]:
    """Turn MCP tool defs into framework Tools (`server.tool` on name collision).

    One unbridgeable schema degrades to PermissiveArgs — never to a crash that
    would lose ALL of the server's tools.
    """
    tools = defs if defs is not None else await client.list_tools()
    seen: set[str] = set()
    out: list[Tool] = []
    for d in tools:
        base = str(d.get("name") or "")
        name = base if base not in seen else f"{client.server_id}.{base}"
        seen.add(base)
        try:
            schema: Type[BaseModel] = json_schema_to_model(d.get("inputSchema") or {"type": "object"}, name=f"Mcp{base}Args")
        except Exception:  # noqa: BLE001 — one bad schema, permissive tool
            schema = PermissiveArgs
        description = d.get("description") or f"MCP tool {base} (via {client.server_id})"
        out.append(define_tool(name=name, description=description, schema=schema, execute=_tool_execute(client, base)))
    return out


def _tool_execute(client: McpClient, tool_name: str) -> Any:
    async def execute(args: Any, ctx: Any) -> str:
        payload: Any
        if isinstance(args, BaseModel):
            # TS zod omits absent keys; pydantic's plain model_dump() fills
            # UNSET optional fields with explicit nulls. Serialize only what
            # the model actually set (model_fields_set) and never a null: an
            # optional arg omitted by the caller is ABSENT from the wire
            # payload, and present-but-null never appears either.
            stored = set(getattr(args, "model_fields_set", None) or ())
            dumped = args.model_dump(exclude_none=True)
            payload = {k: v for k, v in dumped.items() if k in stored} if stored else dumped
        else:
            payload = args
        return await client.call_tool(tool_name, payload)

    return execute


async def connect_mcp_http(url: str, headers: Optional[dict[str, str]] = None, **opts: Any) -> McpHttpClient:
    client = McpHttpClient(url, headers, **opts)
    await client.connect()
    return client


async def connect_mcp_stdio(command: str, args: Optional[list[str]] = None, **opts: Any) -> McpStdioClient:
    client = McpStdioClient(command, args, **opts)
    await client.connect()
    return client