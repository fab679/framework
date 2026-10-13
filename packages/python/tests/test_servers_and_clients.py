"""End-to-end tests for the server/transport parity modules:

a2a_server, mcp_client, observability, tracing, sandbox.

No network, nothing written outside pytest's tmp_path.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from framework_agents import a2a_client
from framework_agents.a2a_server import A2AServer
from framework_agents.agent import Agent, AgentOptions
from framework_agents.errors import AgentError
from framework_agents.mcp_client import (
    McpHttpClient,
    McpStdioClient,
    mcp_tools_to_framework,
)
from framework_agents.models import CompleteOptions, ModelCompletion
from framework_agents.observability import console_events, log_error, supports_color
from framework_agents.sandbox import create_sandbox_backend, run_javascript_tool
from framework_agents.tracing import JsonlSpanSink, MemorySpanSink, tracing_events
from framework_agents.types import TokenUsage


# ---------------------------------------------------------------------------
# A2A server end-to-end (stub Agent + canned provider)
# ---------------------------------------------------------------------------


class StubProvider:
    """Canned completions; advances once per model call (mutable on purpose)."""

    id = "stub"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self._i = 0

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        i = min(self._i, len(self.responses) - 1)
        self._i += 1
        text = self.responses[i]
        if options.on_token is not None:
            options.on_token(text)  # streaming yields token events
        return ModelCompletion(text=text, tool_calls=[], usage=TokenUsage(input_tokens=1, output_tokens=1))


def _make_server(responses: list[str], **kwargs: Any) -> tuple[A2AServer, StubProvider, dict[str, int]]:
    provider = StubProvider(responses)
    created: dict[str, int] = {}

    def factory(context_id: str) -> Agent:
        created[context_id] = created.get(context_id, 0) + 1
        return Agent(AgentOptions(model=provider, name="stub-agent"))

    card = {"name": "stub-agent", "description": "stubbed for tests", "version": "0.1.0"}
    kwargs.setdefault("cors_origins", ["http://front.example"])
    server = A2AServer(card, factory, **kwargs)
    return server, provider, created


def _envelope(text: str, context_id: str | None = None, method: str = "message/send", rpc_id: str = "t-1") -> dict[str, Any]:
    params: dict[str, Any] = {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}}
    if context_id:
        params["contextId"] = context_id
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}


async def test_a2a_server_card_health_options_and_port0() -> None:
    server, _, _ = _make_server(["hello"])
    port = await server.start(host="127.0.0.1", port=0)
    assert port > 0  # port 0 reserved => an ephemeral REAL port came back
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(f"{base}/.well-known/agent-card.json")
            assert res.status_code == 200
            assert res.json()["name"] == "stub-agent"

            # CORS reflects when the Origin matches the allowlist (echoed, not "*")...
            res = await client.get(f"{base}/.well-known/agent-card.json", headers={"origin": "http://front.example"})
            assert res.headers["access-control-allow-origin"] == "http://front.example"
            # ...and stays silent for strangers (or requests with no Origin).
            res = await client.get(f"{base}/.well-known/agent-card.json", headers={"origin": "http://stranger.example"})
            assert "access-control-allow-origin" not in res.headers

            assert (await client.get(f"{base}/health")).json() == {"ok": True}
            assert (await client.get(f"{base}/ok")).json() == {"ok": True}

            res = await client.options(f"{base}/", headers={"origin": "http://front.example"})
            assert res.status_code == 204
            assert res.headers["access-control-allow-origin"] == "http://front.example"

            res = await client.post(f"{base}/nope", json={})
            assert res.status_code == 404
    finally:
        await server.stop()


async def test_a2a_send_envelope_and_context_caching() -> None:
    server, _, created = _make_server(["first", "second", "third"])
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(f"{base}/", json=_envelope("hello"))
            assert res.status_code == 200
            rpc = res.json()
            assert rpc["id"] == "t-1"
            context_id = rpc["result"]["contextId"]
            assert context_id.startswith("ctx-")
            assert rpc["result"]["message"]["parts"] == [{"kind": "text", "text": "first"}]

            # Same contextId: SAME cached agent (fresh response from the SAME stub).
            res2 = await client.post(f"{base}/", json=_envelope("again", context_id=context_id, rpc_id="t-2"))
            assert res2.json()["result"]["message"]["parts"][0]["text"] == "second"
            assert created[context_id] == 1  # LRU cache reused, not re-created

            # A new context gets a DIFFERENT agent created for it.
            res3 = await client.post(f"{base}/", json=_envelope("other", rpc_id="t-3"))
            ctx3 = res3.json()["result"]["contextId"]
            assert ctx3 != context_id
            assert created[ctx3] == 1
            assert res3.json()["result"]["message"]["parts"][0]["text"] == "third"

            # Unknown JSON-RPC method -> -32601 with the method echoed.
            res4 = await client.post(f"{base}/", json=_envelope("x", method="bogus/run", rpc_id="t-4"))
            assert res4.json()["error"]["code"] == -32601
            assert "bogus/run" in res4.json()["error"]["message"]
    finally:
        await server.stop()


async def test_a2a_stream_sse_frames() -> None:
    server, _, created = _make_server(["streamed answer"])
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            frames: list[str] = []
            buffer = ""
            async with client.stream("POST", f"{base}/stream", json=_envelope("stream me", method="message/stream")) as res:
                assert res.status_code == 200
                assert res.headers["content-type"].startswith("text/event-stream")
                async for chunk in res.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if line.startswith("data:"):
                            frames.append(line[len("data:"):].strip())
            parsed = [json.loads(f) if f != "[DONE]" else "[DONE]" for f in frames]
            assert parsed[-1] == "[DONE]"
            context_ids = {p["contextId"] for p in parsed[:-1]}
            assert len(context_ids) == 1
            context_id = context_ids.pop()
            assert context_id.startswith("ctx-")

            events = [p["event"] for p in parsed[:-1] if "event" in p]
            assert [e["type"] for e in events][0] == "step"
            assert "token" in [e["type"] for e in events]

            done_frames = [p for p in parsed[:-1] if "done" in p]
            assert done_frames == [{"contextId": context_id, "done": True, "text": "streamed answer"}]
            assert created[context_id] == 1
    finally:
        await server.stop()


async def test_a2a_stream_disconnect_aborts_run() -> None:
    """The guard task mirrors TS res.on("close"): a disconnect aborts the run."""

    class SlowProvider:
        id = "slow"
        capabilities = {"structured_output": False, "streaming": False}

        def __init__(self) -> None:
            self.cancelled = False
            self.finished = False

        async def complete(self, options: CompleteOptions) -> ModelCompletion:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.finished = True
            return ModelCompletion(text="late", tool_calls=[], usage=TokenUsage())

    slow = SlowProvider()
    server = A2AServer({"name": "slow-agent"}, lambda ctx: Agent(AgentOptions(model=slow, name="slow-agent")))
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", f"{base}/stream", json=_envelope("go", method="message/stream")) as res:
                # Pull the first SSE frame (the step event), then walk away.
                async for _chunk in res.aiter_text():
                    break
        await asyncio.sleep(0.5)  # let the server notice the EOF
        assert slow.cancelled is True
        assert slow.finished is False
    finally:
        await server.stop()


async def test_a2a_context_lru_eviction() -> None:
    server, _, created = _make_server(["cached"] * 10, max_contexts=2)
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            ids = []
            for i in range(3):  # churn through 3 contexts with a 2-slot cache
                res = await client.post(f"{base}/", json=_envelope(f"turn {i}", rpc_id=f"t-{i}"))
                ids.append(res.json()["result"]["contextId"])
            assert len(set(ids)) == 3
            # The two most recent contexts are retained; the oldest was evicted
            # and its agent re-created on the next visit.
            await client.post(f"{base}/", json=_envelope("back to first", context_id=ids[0], rpc_id="back"))
            assert created[ids[0]] == 2  # evicted, then re-created
            await client.post(f"{base}/", json=_envelope("still here", context_id=ids[2], rpc_id="keep"))
            assert created[ids[2]] == 1  # hot: never evicted
    finally:
        await server.stop()


async def test_a2a_body_too_large_rejected_413() -> None:
    server, _, _ = _make_server(["x"], max_body_bytes=64)
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(f"{base}/", json=_envelope("way more than sixty four bytes of body, surely"))
            assert res.status_code == 413
            body = res.json()
            assert body["id"] is None
            assert body["error"]["code"] == -32603
            assert body["error"]["message"].startswith("Request body exceeds")
    finally:
        await server.stop()


async def test_a2a_malformed_body_is_jsonrpc_error() -> None:
    server, _, _ = _make_server(["x"])
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(f"{base}/", content=b"{not json", headers={"content-type": "application/json"})
            assert res.status_code == 200
            body = res.json()
            assert body["id"] is None
            assert body["error"]["code"] == -32603
    finally:
        await server.stop()


async def test_a2a_stop_closes_socket() -> None:
    server, _, _ = _make_server(["x"])
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient() as client:
        await client.get(f"{base}/health")  # alive
        await server.stop()
        with pytest.raises(httpx.TransportError):
            await client.get(f"{base}/health")


async def test_a2a_client_roundtrip_against_server() -> None:
    """The existing wire client must interop with the new server."""
    server, _, _ = _make_server(["hello over the wire", "and again"])
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        remote = a2a_client.A2AClient(base)
        card = await remote.get_card()
        assert card["name"] == "stub-agent"
        first = await remote.send("hi")
        assert first["text"] == "hello over the wire"
        assert first["contextId"] and first["contextId"].startswith("ctx-")
        second = await remote.send("again", context_id=first["contextId"])
        assert second["contextId"] == first["contextId"]
        assert second["text"] == "and again"
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# MCP stdio client (in-test subprocess server under tmp_path)
# ---------------------------------------------------------------------------

_TINY_MCP = r'''
import json
import sys

INIT_LOG = sys.argv[1]

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

TOOLS = [
    {
        "name": "word_count",
        "description": "Count words in text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "hang",
        "description": "Never responds.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    if method == "initialize":
        with open(INIT_LOG, "w") as fh:
            json.dump({"clientInfo": msg.get("params", {}).get("clientInfo")}, fh)
        send({
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "tiny-mcp", "version": "0.0.1"},
            },
        })
    elif method == "notifications/initialized":
        pass  # notification: never respond
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = msg.get("params", {}).get("name")
        if name == "hang":
            continue  # in-flight forever: close()/exit must settle it
        args = msg.get("params", {}).get("arguments") or {}
        text = str(args.get("text", ""))
        send({
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"content": [{"type": "text", "text": str(len(text.split()))}]},
        })
'''


async def test_mcp_stdio_client(tmp_path: Any) -> None:
    script = tmp_path / "tiny_mcp.py"
    script.write_text(_TINY_MCP, encoding="utf-8")
    init_log = tmp_path / "init.json"

    client = McpStdioClient(sys.executable, [str(script), str(init_log)])
    try:
        await client.connect()

        # ClientInfo must be the distinctive client name, never personal.
        assert json.loads(init_log.read_text())["clientInfo"] == {"name": "framework-agents", "version": "0.1.0"}

        defs = await client.list_tools()
        assert [d["name"] for d in defs] == ["word_count", "hang"]

        assert await client.call_tool("word_count", {"text": "one two three four"}) == "4"

        # JSON Schema -> pydantic bridge validates and executes.
        bridged = await mcp_tools_to_framework(client)
        assert [t.name for t in bridged] == ["word_count", "hang"]
        word_count = bridged[0]
        assert word_count.schema.model_validate({"text": "a b c"}).text == "a b c"
        with pytest.raises(ValidationError):
            word_count.schema.model_validate({"text": 5})
        args = word_count.schema.model_validate({"text": "a b c"})
        assert await word_count.execute(args, None) == "3"
    finally:
        await client.close()


async def test_mcp_stdio_close_rejects_pending(tmp_path: Any) -> None:
    script = tmp_path / "tiny_mcp.py"
    script.write_text(_TINY_MCP, encoding="utf-8")
    init_log = tmp_path / "init2.json"

    client = McpStdioClient(sys.executable, [str(script), str(init_log)])
    await client.connect()
    pending = asyncio.ensure_future(client.call_tool("hang", {}))
    await asyncio.sleep(0.25)  # request reached the server (which sits on it)
    await client.close()  # must settle the in-flight request, never hang
    with pytest.raises(AgentError) as excinfo:
        await pending
    assert excinfo.value.code == "MCP_TRANSPORT"
    assert excinfo.value.retryable is True


async def test_mcp_stdio_process_exit_rejects_pending(tmp_path: Any) -> None:
    script = tmp_path / "tiny_mcp.py"
    script.write_text(_TINY_MCP, encoding="utf-8")
    init_log = tmp_path / "init3.json"

    client = McpStdioClient(sys.executable, [str(script), str(init_log)])
    await client.connect()
    assert client.proc is not None
    try:
        pending = asyncio.ensure_future(client.call_tool("hang", {}))
        await asyncio.sleep(0.25)
        client.proc.kill()  # crash/OOM/kill: 'exit' must settle in-flight calls
        with pytest.raises(AgentError) as excinfo:
            await pending
        assert excinfo.value.code == "MCP_TRANSPORT"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# MCP HTTP client (httpx MockTransport — no live server)
# ---------------------------------------------------------------------------

_TOOLS_LIST = [
    {
        "name": "echo",
        "description": "Echo tool.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


async def test_mcp_http_client_mock_transport() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        headers = {k.lower(): v for k, v in request.headers.items()}
        seen.append({"body": body, "headers": headers})
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "sess-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "mock-mcp", "version": "9.9"},
                    },
                },
            )
        if method == "notifications/initialized":
            # Streamable servers answer notifications with SSE — the client
            # must read and DISCARD it, never hang waiting on a match.
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='data: {"jsonrpc":"2.0","result":{}}\n\ndata: [DONE]\n\n',
            )
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": _TOOLS_LIST}})
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {"content": [{"type": "text", "text": "boom, tool exploded"}], "isError": True},
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mcp") as http:
        mcp = McpHttpClient("http://mcp/rpc", http_client=http)
        try:
            info = await mcp.connect()
            assert info == {"name": "mock-mcp", "version": "9.9"}

            notifications = [s for s in seen if s["body"].get("method") == "notifications/initialized"]
            assert len(notifications) == 1
            assert "id" not in notifications[0]["body"]  # notifications carry NO id

            defs = await mcp.list_tools()
            assert [d["name"] for d in defs] == ["echo"]

            requests_after_init = [s for s in seen if s["body"].get("method") in ("tools/list",)]
            assert requests_after_init[0]["headers"]["mcp-session-id"] == "sess-1"

            with pytest.raises(AgentError) as excinfo:
                await mcp.call_tool("echo", {"text": "hi"})
            assert excinfo.value.code == "MCP_TOOL_ERROR"
            assert excinfo.value.retryable is False
            assert "boom, tool exploded" in str(excinfo.value)

            # sessionId was captured from the initialize response header.
            assert mcp._session_id == "sess-1"  # noqa: SLF001 - asserted on purpose
        finally:
            await mcp.close()


async def test_mcp_bridge_bad_enum_degrades(tmp_path: Any) -> None:
    """One unbridgeable enum schema degrades to permissive, never nukes all tools."""

    class FakeMcpServer:
        server_id = "fake"

        async def list_tools(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": "good",
                    "description": "fine",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "level": {"enum": [1, 2, 3]},
                            "mode": {"enum": ["fast", "slow"]},
                        },
                        "required": ["text"],
                    },
                },
                {
                    "name": "bad",
                    "description": "unbridgeable enum",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"x": {"enum": [{"unhashable": True}]}},
                        "required": ["x"],
                    },
                },
            ]

        async def call_tool(self, name: str, args: Any) -> str:
            return f"{name}:{json.dumps(args)}"

        async def close(self) -> None:
            return None

    tools = await mcp_tools_to_framework(FakeMcpServer())
    assert [t.name for t in tools] == ["good", "bad"]

    good = tools[0]
    validated = good.schema.model_validate({"text": "hi", "level": 2, "mode": "slow"})
    assert validated.level == 2 and validated.mode == "slow"
    with pytest.raises(ValidationError):
        good.schema.model_validate({"text": "hi", "level": 5})

    bad = tools[1]
    # Permissive degrade: everything passes.
    assert "x" in bad.schema.model_validate({"x": 12345}).model_dump()


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------


def test_console_events_step_and_done_lines(capsys: Any) -> None:
    events = console_events(prefix="[observer]", colors=False)
    events.on_step(0)
    events.on_token("hi ")
    events.on_tool_call("search", {"q": "x"})
    events.on_tool_result("search", "found it")
    events.on_done("finished work")

    out = capsys.readouterr().out
    assert "[observer] step 0" in out
    assert "[observer] tool:call search" in out
    assert "[observer] tool:result search" in out
    assert "hi " in out
    assert "[observer]" in out
    assert "done:" in out
    assert "finished work" in out


def test_console_events_no_color_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert supports_color() is False
    monkeypatch.delenv("NO_COLOR")
    # pytest's captured stdout is not a tty either.
    assert supports_color() is False


def test_log_error_prints_code_and_message(capsys: Any) -> None:
    log_error("[observer]", AgentError("BOOM_CODE", "it broke badly"), colors=False)
    err = capsys.readouterr().err
    assert "[observer] error:" in err
    assert "[BOOM_CODE]" in err
    assert "it broke badly" in err


# ---------------------------------------------------------------------------
# tracing
# ---------------------------------------------------------------------------


def test_tracing_events_memory_sink() -> None:
    sink = MemorySpanSink()
    events = tracing_events(sink, trace_id="trace-42")

    events.on_step(0)
    events.on_token("hello ")
    events.on_thinking("hmm ")
    events.on_tool_call("search", {"q": "x"})
    events.on_tool_result("search", "output")
    events.on_done("done text")

    names = [s.name for s in sink.spans]
    assert names == [
        "step.start",
        "model.token",
        "model.thinking",
        "tool.call",
        "tool.result",
        "run.done",
    ]
    assert all(s.trace_id == "trace-42" for s in sink.spans)
    assert all(s.started_at and s.ended_at for s in sink.spans)  # duration-complete
    assert len({s.span_id for s in sink.spans}) == 6  # unique sequential ids
    tool_call = sink.spans[3]
    tool_result = sink.spans[4]
    assert tool_result.parent_id == tool_call.span_id
    done = sink.spans[5]
    assert done.attributes == {"text": "done text", "toolCalls": 0}


def test_tracing_parent_links_to_latest_tool_call() -> None:
    sink = MemorySpanSink()
    events = tracing_events(sink, trace_id="trace-q")

    events.on_tool_call("first", {})
    events.on_tool_call("second", {})
    events.on_tool_result("second", "out")

    first_call, second_call, result = sink.spans
    assert result.parent_id == second_call.span_id  # the LAST call, not name-keyed
    assert result.parent_id != first_call.span_id


def test_tracing_jsonl_sink_writes_lines(tmp_path: Any) -> None:
    path = tmp_path / "nested" / "deeper" / "spans.jsonl"
    sink = JsonlSpanSink(str(path))  # mkdir -p
    events = tracing_events(sink, trace_id="trace-j")
    events.on_step(0)
    events.on_tool_call("run", {"a": 1})
    events.on_tool_result("run", "ok")
    events.on_done("end")

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["name"] == "step.start"
    assert parsed[1]["name"] == "tool.call"
    assert parsed[2]["parentId"] == parsed[1]["spanId"]
    assert parsed[3]["name"] == "run.done"
    assert all(p["traceId"] == "trace-j" for p in parsed)


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------


async def test_run_javascript_computes_expression() -> None:
    tool = run_javascript_tool()
    args = tool.schema.model_validate({"code": "6 * 7"})
    assert await tool.execute(args, None) == "42"


async def test_run_javascript_print_and_result() -> None:
    tool = run_javascript_tool()
    args = tool.schema.model_validate({"code": "print('hello');\n6 * 7"})
    out = await tool.execute(args, None)
    assert "hello" in out
    assert "42" in out


async def test_run_javascript_timeout() -> None:
    tool = run_javascript_tool(timeout_ms=150)
    args = tool.schema.model_validate({"code": "sleep(5)"})
    with pytest.raises(AgentError) as excinfo:
        await tool.execute(args, None)
    assert excinfo.value.code == "SANDBOX_TIMEOUT"
    assert excinfo.value.retryable is True


async def test_run_javascript_syntax_error() -> None:
    tool = run_javascript_tool()
    args = tool.schema.model_validate({"code": "def not javascript("})
    with pytest.raises(AgentError) as excinfo:
        await tool.execute(args, None)
    assert excinfo.value.code == "SANDBOX_ERROR"


async def test_sandbox_execute_roundtrip(tmp_path: Any, monkeypatch: Any) -> None:
    backend = create_sandbox_backend(str(tmp_path), timeout_ms=10000)
    (execute,) = backend.tools()
    assert execute.name == "execute"

    # touch + cat roundtrip inside the root dir (cwd jail)
    args = execute.schema.model_validate({"command": "printf hi > f.txt && cat f.txt"})
    out = await execute.execute(args, None)
    assert out.strip() == "hi"
    assert (tmp_path / "f.txt").read_text() == "hi"

    # stderr is captured with a separator
    args = execute.schema.model_validate({"command": "echo oops >&2"})
    out = await execute.execute(args, None)
    assert "[stderr]" in out
    assert "oops" in out

    # env scrub: inherited secrets never reach the sandbox
    monkeypatch.setenv("SECRET_TOKEN", "leaky-value")
    args = execute.schema.model_validate({"command": "echo token=$SECRET_TOKEN"})
    out = await execute.execute(args, None)
    assert "leaky-value" not in out

    # failures report the exit code
    args = execute.schema.model_validate({"command": "exit 3"})
    out = await execute.execute(args, None)
    assert "[Command failed with exit code 3]" in out

    # silent success still says something useful
    args = execute.schema.model_validate({"command": "true"})
    assert await execute.execute(args, None) == "[Command succeeded with exit code 0]"


async def test_sandbox_execute_timeout(monkeypatch: Any, tmp_path: Any) -> None:
    backend = create_sandbox_backend(str(tmp_path), timeout_ms=150)
    (execute,) = backend.tools()
    args = execute.schema.model_validate({"command": "sleep 5"})
    with pytest.raises(AgentError) as excinfo:
        await execute.execute(args, None)
    assert excinfo.value.code == "SANDBOX_TIMEOUT"
    assert excinfo.value.retryable is True


async def test_sandbox_execute_output_truncation_spill(tmp_path: Any) -> None:
    backend = create_sandbox_backend(str(tmp_path), timeout_ms=10000, max_output_chars=50)
    (execute,) = backend.tools()
    args = execute.schema.model_validate({"command": "seq 1 200 | tr '\\n' ' '"})
    out = await execute.execute(args, None)
    assert "truncated" in out
    spills = list(tmp_path.glob(".spill-*.txt"))
    assert spills, "the full output must spill to a file in the workspace"