"""Tests for the framework.json serve layer: config load, env precedence,
port assignment, A2A endpoints (card / health / CORS / 404), lazy agent
building, and checkpointer wiring.

No network egress (everything binds 127.0.0.1 with port 0), no real API
keys (agents are built lazily per context, so nothing ever calls the
model at boot).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from framework_agents.checkpoint import FileCheckpointer
from framework_agents.models import CompleteOptions, ModelCompletion
from framework_agents.serve import (
    AgentDef,
    FrameworkApp,
    build_served_agent,
    load_framework_config,
    serve_framework,
)
from framework_agents.types import TokenUsage


class StubModel:
    """Canned one-shot provider so factory-time wiring can be tested offline."""

    id = "stub"
    capabilities = {"structured_output": False, "streaming": False}

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        return ModelCompletion(text="ok", tool_calls=[], usage=TokenUsage(input_tokens=1, output_tokens=1))


def _write_framework(tmp_path: Path, agents: list[dict], extra: dict | None = None) -> Path:
    config: dict = {
        "server": {"host": "127.0.0.1", "basePort": 0},
        "http": {"corsOrigins": ["http://test"]},
        "checkpointer": {"dir": "checkpoints"},
        "agents": agents,
    }
    if extra:
        config.update(extra)
    path = tmp_path / "framework.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _two_agents() -> list[dict]:
    return [
        {"name": "alpha", "model": "together:whatever", "description": "first agent"},
        {"name": "beta", "model": "together:whatever"},
    ]


async def test_serve_framework_ports_card_health_cors_and_stop(tmp_path: Path) -> None:
    config_path = _write_framework(tmp_path, _two_agents())
    app = await serve_framework(str(config_path))
    try:
        assert isinstance(app, FrameworkApp)
        assert [a.name for a in app.agents] == ["alpha", "beta"]

        # Sequential port assignment: first agent binds first; the second's
        # port continues at the first's actual port + 1.
        ports = [int(a.url.rsplit(":", 1)[1]) for a in app.agents]
        assert ports[0] > 0
        assert ports[1] >= ports[0] + 1
        for agent in app.agents:
            assert agent.card_url == f"{agent.url}/.well-known/agent-card.json"

        # Lazy build: nothing ran a model resolve at boot.
        for entry in app.agents:
            assert entry.server._contexts == {}

        async with httpx.AsyncClient(timeout=5.0) as client:
            for entry in app.agents:
                # Card: agent name matches.
                card = (await client.get(entry.card_url)).json()
                assert card["name"] == entry.name

                # Health endpoints.
                assert (await client.get(f"{entry.url}/ok")).json() == {"ok": True}
                assert (await client.get(f"{entry.url}/health")).status_code == 200

                # CORS preflight echoes the allowed origin.
                preflight = await client.options(entry.url, headers={"Origin": "http://test"})
                assert preflight.status_code == 204
                assert preflight.headers["access-control-allow-origin"] == "http://test"

                # Preflight without an origin: no CORS headers back.
                bare = await client.options(entry.url)
                assert bare.status_code == 204
                assert "access-control-allow-origin" not in bare.headers

                # Unknown paths 404.
                missing = await client.get(f"{entry.url}/nope")
                assert missing.status_code == 404
                assert missing.json() == {"error": "not found"}
    finally:
        await app.stop()

    # stop() closed both servers — connections now refuse.
    for entry in app.agents:
        with pytest.raises(httpx.ConnectError):
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.get(entry.card_url)


async def test_serve_framework_wires_file_checkpointer(tmp_path: Path) -> None:
    config_path = _write_framework(tmp_path, _two_agents())
    app = await serve_framework(load_framework_config(str(config_path)))
    try:
        expected_root = Path(tmp_path / "checkpoints").resolve()
        for entry in app.agents:
            assert isinstance(entry.server.checkpointer, FileCheckpointer)
            assert entry.server.checkpointer.root == expected_root
        # Shared checkpointer across the fleet.
        assert app.agents[0].server.checkpointer is app.agents[1].server.checkpointer
    finally:
        await app.stop()
    # No checkpointer configured -> server side is None.
    plain_path = tmp_path / "framework2.json"
    plain_path.write_text(json.dumps({
        "server": {"basePort": 0},
        "agents": _two_agents(),
    }), encoding="utf-8")
    app2 = await serve_framework(str(plain_path))
    try:
        for entry in app2.agents:
            assert entry.server.checkpointer is None
    finally:
        await app2.stop()


async def test_explicit_agent_port_then_incremental(tmp_path: Path) -> None:
    agents = [
        {"name": "pinned", "model": "together:whatever", "port": 8910},
        {"name": "floating", "model": "together:whatever"},
    ]
    config_path = _write_framework(tmp_path, agents, extra={"server": {"host": "127.0.0.1", "basePort": 8901}})
    app = await serve_framework(str(config_path))
    try:
        assert app.agents[0].url.endswith(":8910")
        # Next agent continues at the previous ACTUAL port + 1.
        assert app.agents[1].url.endswith(":8911")
    finally:
        await app.stop()


async def test_env_file_precedence_never_clobbers_real_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real environment value must survive both env files (setdefault).
    monkeypatch.setenv("FW_SERVE_TEST_SYNC", "real-env-value")
    (tmp_path / ".env").write_text("FW_SERVE_TEST_SYNC=first\n", encoding="utf-8")
    (tmp_path / "agent.env").write_text("FW_SERVE_TEST_SYNC=second\n", encoding="utf-8")
    agents = [
        {"name": "loader", "model": "together:whatever", "envFile": "agent.env"},
        {"name": "plain", "model": "together:whatever"},
    ]
    config_path = _write_framework(tmp_path, agents, extra={"envFile": ".env"})
    try:
        load_framework_config(str(config_path))
        assert os.environ["FW_SERVE_TEST_SYNC"] == "real-env-value"

        # With the key absent, the framework-level file loads first, and the
        # per-agent file (also setdefault) cannot replace it.
        monkeypatch.delenv("FW_SERVE_TEST_SYNC", raising=False)
        load_framework_config(str(config_path))
        assert os.environ.get("FW_SERVE_TEST_SYNC") == "first"
    finally:
        os.environ.pop("FW_SERVE_TEST_SYNC", None)

    # The inline `env` mapping DOES override (applied after env files).
    config2 = tmp_path / "framework2.json"
    config2.write_text(json.dumps({
        "server": {"basePort": 0},
        "env": {"FW_SERVE_TEST_INLINE": "mapped"},
        "agents": [{"name": "x", "model": "together:whatever"}],
    }), encoding="utf-8")
    try:
        load_framework_config(str(config2))
        assert os.environ.get("FW_SERVE_TEST_INLINE") == "mapped"
    finally:
        os.environ.pop("FW_SERVE_TEST_INLINE", None)


def test_validate_path_load_framework_config_only(tmp_path: Path) -> None:
    config_path = _write_framework(
        tmp_path,
        [
            {"name": "alpha", "model": "together:whatever", "port": 8500, "envFile": "alpha.env"},
            {"file": "agents/beta.json", "description": "overridden"},
        ],
    )
    (tmp_path / "alpha.env").write_text("A=1\n", encoding="utf-8")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "beta.json").write_text(json.dumps({"name": "beta", "model": "openai:zz"}), encoding="utf-8")

    loaded = load_framework_config(str(config_path))
    assert loaded.server.host == "127.0.0.1"
    assert loaded.server.base_port == 0
    assert loaded.http.cors_origins == ["http://test"]
    assert [a.name for a in loaded.agents] == ["alpha", "beta"]
    assert loaded.agents[0].model == "together:whatever"
    assert loaded.agents[0].port == 8500
    assert loaded.agents[0].env_file == str((tmp_path / "alpha.env").resolve())
    # File-ref agent: inline overrides win; defaults apply to the rest.
    assert loaded.agents[1].model == "openai:zz"
    assert loaded.agents[1].description == "overridden"
    assert loaded.agents[1].max_steps == 8

    # Bad config: agents must be a non-empty array.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"server": {"basePort": 0}, "agents": []}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_framework_config(str(bad))

    # An agent def without a model is invalid (inline branch), even with a
    # `file` key pointing nowhere.
    missing = tmp_path / "missingmodel.json"
    missing.write_text(json.dumps({
        "server": {"basePort": 0},
        "agents": [{"name": "no-model", "file": "./ghost.json"}],
    }), encoding="utf-8")
    with pytest.raises((ValidationError, OSError, json.JSONDecodeError)):
        load_framework_config(str(missing))


def test_cli_validate_command_prints_agents(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from framework_agents.serve import main

    config_path = _write_framework(tmp_path, _two_agents())
    code = main(["validate", "-c", str(config_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert f"OK: {str(config_path)}" in out
    assert "- alpha [together:whatever]" in out
    assert "- beta [together:whatever]" in out


def test_cli_validate_reports_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from framework_agents.serve import main

    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    code = main(["validate", "-c", str(broken)])
    assert code == 1
    assert "config error" in capsys.readouterr().err


def _skill_dir(tmp_path: Path) -> str:
    skills = tmp_path / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill pack\n---\nBody of the skill.\n",
        encoding="utf-8",
    )
    return str(tmp_path / "skills")


async def test_build_served_agent_wires_skills_middleware_and_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import framework_agents.serve as serve_mod

    monkeypatch.setattr(serve_mod, "init_chat_model", lambda spec: StubModel())
    defn = AgentDef(
        name="alpha",
        model="together:whatever",
        skills=[_skill_dir(tmp_path)],
        filesystem=str(tmp_path),
        todos=True,
        session_dir=str(tmp_path / "sessions"),
        max_steps=3,
    )
    built = await build_served_agent(defn, session_id="alpha:ctx-7")
    agent = built.agent
    prompt = agent.options.system_prompt
    assert "Available skill packs (mention $name to invoke):" in prompt
    assert "- demo: demo skill pack" in prompt
    assert agent.options.name == "alpha"
    assert agent.options.max_steps == 3
    # filesystem + todos middlewares (both exist in this codebase).
    assert len(agent.options.middleware) == 2
    assert built.card == {"name": "alpha"}  # no description -> absent
    # Session manager reserved on the instance, context-scoped id.
    assert agent.session_manager.session_id == "alpha:ctx-7"

    # No description given -> card reflects only the name; with one set...
    built2 = await build_served_agent(
        AgentDef(name="beta", model="together:whatever", description="second"),
    )
    assert built2.card == {"name": "beta", "description": "second"}


async def test_served_agent_built_fresh_per_context_with_context_scoped_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import framework_agents.serve as serve_mod

    monkeypatch.setattr(serve_mod, "init_chat_model", lambda spec: StubModel())
    config_path = tmp_path / "framework.json"
    config_path.write_text(json.dumps({
        "server": {"basePort": 0},
        "agents": [{"name": "alpha", "model": "together:whatever", "sessionDir": "./sessions"}],
    }), encoding="utf-8")
    app = await serve_framework(str(config_path))
    try:
        entry = app.agents[0]
        envelope = {
            "jsonrpc": "2.0",
            "id": "t-1",
            "method": "message/send",
            "params": {
                "contextId": "ctx-7",
                "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
            },
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(entry.url, json=envelope)
        result = resp.json()["result"]
        assert result["contextId"] == "ctx-7"
        assert result["message"]["role"] == "agent"
        # The built agent is cached per context and carries the pinned
        # "{name}:{contextId}" session structure.
        built_agents = list(entry.server._contexts.values())
        assert len(built_agents) == 1
        assert built_agents[0].session_manager.session_id == "alpha:ctx-7"
    finally:
        await app.stop()