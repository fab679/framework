"""LIVE: framework.json-driven serving — write a config, boot serve_framework,
hit it over the A2A wire, answer a real question, shut down cleanly.

The framework.json format is IDENTICAL to the TypeScript CLI's — this example
boots the same file shape from Python (agents are built fresh per contextId,
models lazy so booting needs no API key)."""
import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from framework_agents.serve import load_framework_config, serve_framework

CONFIG = {
    "server": {"host": "127.0.0.1", "basePort": 0},
    "http": {"corsOrigins": []},
    "agents": [
        {
            "name": "greeter",
            "description": "Greets people by name",
            "model": "together:__MODEL__",
            "maxSteps": 4,
        }
    ],
}

async def main():
    cfg_model = __import__("framework_agents").load_config().together_model
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "framework.json"
        path.write_text(json.dumps(CONFIG).replace("__MODEL__", cfg_model))
        file = load_framework_config(str(path))
        print("CONFIG AGENTS:", [a.name for a in file.agents])

        app = await serve_framework(file)
        try:
            [served] = app.agents
            url = served.url
            print("SERVED:", served.name, "->", url)
            async with httpx.AsyncClient() as client:
                card = (await client.get(f"{url}/.well-known/agent-card.json")).json()
                print("REMOTE CARD:", card["name"])
                # The wire contract: JSON-RPC envelope the way the TS server expects.
                envelope = {
                    "jsonrpc": "2.0", "id": "e1", "method": "message/send",
                    "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "The capital of Spain? One word."}]}},
                }
                res = await client.post(url, json=envelope)
                rpc = res.json()
                text = rpc["result"]["message"]["parts"][0]["text"]
                print("REMOTE SEND:", text.strip()[:40], "| contextId:", rpc["result"].get("contextId") is not None, "| Madrid expected")
                print("PASS: framework.json -> serve -> remote A2A" if "madrid" in text.lower() else "NOTE: model variance")
        finally:
            await app.stop()
            print("STOPPED cleanly")

asyncio.run(main())
