"""Cross-language conformance: the Python A2A client vs the TS A2A server.

This is the roadmap's "cross-language conformance" promise, made executable:
one process runs the TypeScript server (stub model — no network), another
speaks to it from Python over the exact wire contract (JSON-RPC envelope,
SSE frames, [DONE], contextId chain). Skipped when the TS toolchain is
absent so plain-python environments stay green.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

from framework_agents import AgentClient, A2AClient  # noqa: E402

TS_DIR = pathlib.Path(__file__).parents[2] / "typescript"
CROSS = os.environ.get("CROSS_LANG", "1") == "1"

pytestmark = pytest.mark.skipif(
    not (TS_DIR / "src" / "a2a" / "index.ts").exists() or not CROSS,
    reason="TS package or toolchain absent (set CROSS_LANG=0 to force-skip)",
)

TS_SERVER_SCRIPT = """
import { A2AServer, MockModelProvider, Agent } from "{src}/index.js";

const server = new A2AServer({
  card: { name: "ts-side", description: "cross-language conformance target" },
  agentFactory: () => new Agent({ name: "stub", model: new MockModelProvider() }),
});
const port = await server.start();
console.log("PORT:" + port);
// Keep the process alive until the parent kills it.
setInterval(() => {}, 10_000);
"""


@pytest.fixture
async def ts_server_port():
    root = TS_DIR / "src" / "index.js"
    script = TS_SERVER_SCRIPT.replace("{src}", str(root.parent))
    script_path = TS_DIR / "target_ts_conformance.ts"
    script_path.write_text(script)
    try:
        proc = subprocess.Popen(
            ["npx", "--no-install", "tsx", str(script_path)],
            cwd=str(TS_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        port: int | None = None
        output: list[str] = []
        for _ in range(400):  # up to ~40s for boot + tsx warmup
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("PORT:"):
                port = int(line.split(":", 1)[1].strip())
                break
            output.append(line)
        if port is None:
            proc.kill()
            pytest.skip(
                "TS server failed to boot (toolchain/tsx missing?): "
                + ("\n".join(output) or "<no output>")[:300]
            )
        yield port
        proc.kill()
    finally:
        script_path.unlink(missing_ok=True)


async def test_python_client_gets_ts_card(ts_server_port):
    async with httpx.AsyncClient() as client:
        res = await client.get(f"http://127.0.0.1:{ts_server_port}/.well-known/agent-card.json")
    assert res.status_code == 200
    assert res.json()["name"] == "ts-side"


async def test_python_a2a_send_against_ts_server(ts_server_port):
    rpc = A2AClient(f"http://127.0.0.1:{ts_server_port}")
    reply = await rpc.send("hello")
    assert reply["text"].startswith("mock-response")
    assert reply["contextId"], "server-assigned contextId must round-trip through result"
    second = await rpc.send("again", reply["contextId"])
    assert second["contextId"] == reply["contextId"]


async def test_python_stream_client_against_ts_server(ts_server_port):
    """The envelope bug (client POSTing without JSON-RPC params) would make
    this hang or stream a run over an EMPTY message — the strongest pin."""
    contexts: list[str] = []
    types: list[str] = []
    async for event in AgentClient(f"http://127.0.0.1:{ts_server_port}").stream(
        "cross-language streams",
        None,
        None,
        on_context=contexts.append,
    ):
        types.append(event["type"])
        if event["type"] == "done":
            assert event["text"].startswith("mock-response")
    assert "done" in types
    assert contexts, "contextId must arrive in the SSE frames"