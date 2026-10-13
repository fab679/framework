"""Sessions: persist agent state + history across restarts.

Parity: src/sessions/index.ts.

- Storage: byte-moving backends (memory / local files).
- SessionManager: latest snapshot + immutable checkpoints (UUID ids).
- Agent snapshots: manual take/load with field selection + appData.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .errors import AgentError
from .types import ChatMessage, deserialize_messages, serialize_messages


class Storage(Protocol):
    async def read(self, key: str) -> str | None: ...
    async def write(self, key: str, value: str) -> None: ...
    async def delete_prefix(self, prefix: str) -> None: ...
    async def list(self, prefix: str) -> list[str]: ...


class InMemoryStorage:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def read(self, key: str) -> str | None:
        return self._data.get(key)

    async def write(self, key: str, value: str) -> None:
        self._data[key] = value

    async def delete_prefix(self, prefix: str) -> None:
        for k in [k for k in self._data if k.startswith(prefix)]:
            del self._data[k]

    async def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))


class LocalFileStorage:
    """File-per-key under `root`."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        # Trailing/duplicate slashes are harmless (they only yield empty
        # segments); only traversal segments or NULs can escape the root.
        segments = [seg for seg in key.split("/") if seg != ""]
        if ".." in segments or "\0" in key:
            raise AgentError("STORAGE_KEY_INVALID", f"Invalid storage key: {json.dumps(key)}", False)
        return self.root.joinpath(*segments)

    async def read(self, key: str) -> str | None:
        try:
            return self._path(key).read_text(encoding="utf-8")
        except OSError:
            return None

    async def write(self, key: str, value: str) -> None:
        full = self._path(key)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(value, encoding="utf-8")

    async def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix)
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass

    async def list(self, prefix: str) -> list[str]:
        out: list[str] = []
        base = self._path(prefix)

        def walk(directory: Path, rel: str) -> None:
            try:
                entries = list(os.scandir(directory))
            except OSError:
                return  # missing dir / path is a file: nothing to list
            for entry in sorted(entries, key=lambda e: e.name):
                if entry.is_dir():
                    walk(Path(entry.path), f"{rel}{entry.name}/")
                else:
                    out.append(f"{prefix}{rel}{entry.name}")

        walk(base, "")
        return sorted(out)


class SnapshotData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    messages: list[ChatMessage] | None = None
    state: dict[str, Any] | None = None
    system_prompt: str | None = Field(default=None, serialization_alias="systemPrompt")

    def dump(self) -> dict[str, Any]:
        """JSON-safe view for storage (absent fields stay absent, as TS)."""
        data: dict[str, Any] = {}
        if self.messages is not None:
            data["messages"] = serialize_messages(self.messages)
        if self.state is not None:
            data["state"] = self.state
        if self.system_prompt is not None:
            data["systemPrompt"] = self.system_prompt
        return data


SnapshotField = Literal["messages", "state", "systemPrompt"]

SESSION_PRESET: tuple[SnapshotField, ...] = ("messages", "state")


def select_snapshot_fields(
    full: SnapshotData,
    preset: Literal["session"] | None = None,
    include: list[SnapshotField] | None = None,
    exclude: list[SnapshotField] | None = None,
) -> SnapshotData:
    """Preset picks the base bundle; include/exclude adjust on top (from
    the EMPTY base when no preset is given — an include-only selection
    picks exactly those fields)."""
    base: set[SnapshotField] = set(SESSION_PRESET if preset == "session" else ())
    base.update(include or [])
    base.difference_update(exclude or [])
    data = SnapshotData()
    if "messages" in base:
        data.messages = full.messages
    if "state" in base:
        data.state = full.state
    if "systemPrompt" in base:
        data.system_prompt = full.system_prompt
    return data


@dataclass
class Snapshot:
    id: str
    created_at: str
    data: SnapshotData
    app_data: dict[str, Any] | None = None


def _uuid() -> str:
    return str(uuid.uuid4())


class SessionManager:
    """Latest snapshot + immutable checkpoints, keyed per session/agent."""

    def __init__(self, storage: Storage, session_id: str | None = None, agent_id: str = "default") -> None:
        self.session_id = session_id or _uuid()
        self.agent_id = agent_id
        self._storage = storage

    def scope(self) -> str:
        return f"session/{self.session_id}/scopes/agent/{self.agent_id}/snapshots"

    async def save_latest(self, data: SnapshotData) -> None:
        await self._storage.write(f"{self.scope()}/snapshot_latest.json", json.dumps({"data": data.dump()}))

    async def load_latest(self) -> SnapshotData | None:
        raw = await self._storage.read(f"{self.scope()}/snapshot_latest.json")
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)["data"]
            return SnapshotData.model_validate(_snapshot_fields(parsed))
        except (ValueError, KeyError, TypeError):
            return None

    async def save_snapshot(self, data: SnapshotData, app_data: dict[str, Any] | None = None) -> str:
        """Immutable checkpoint; returns its id."""
        id = _uuid()
        snapshot = Snapshot(id=id, created_at=_now_iso(), data=data, app_data=app_data)
        payload: dict[str, Any] = {
            "id": id,
            "createdAt": snapshot.created_at,
            "data": data.dump(),
        }
        if app_data is not None:
            payload["appData"] = app_data
        await self._storage.write(
            f"{self.scope()}/immutable_history/snapshot_{id}.json", json.dumps(payload)
        )
        return id

    async def list_snapshot_ids(self) -> list[str]:
        keys = await self._storage.list(f"{self.scope()}/immutable_history/")
        ids = []
        for key in keys:
            match = re.search(r"snapshot_(.+)\.json$", key)
            if match:
                ids.append(match.group(1))
        return sorted(ids)

    async def restore_snapshot(self, id: str) -> Snapshot | None:
        raw = await self._storage.read(f"{self.scope()}/immutable_history/snapshot_{id}.json")
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
            data = SnapshotData.model_validate(_snapshot_fields(parsed["data"]))
            return Snapshot(
                id=parsed["id"],
                created_at=parsed["createdAt"],
                data=data,
                app_data=parsed.get("appData"),
            )
        except (ValueError, KeyError, TypeError):
            return None

    async def delete_session(self) -> None:
        await self._storage.delete_prefix(f"session/{self.session_id}/")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _snapshot_fields(raw: Any) -> dict[str, Any]:
    """Validated kwargs for SnapshotData from stored JSON (absent fields
    stay absent rather than becoming Nones)."""
    fields: dict[str, Any] = {}
    if not isinstance(raw, dict):
        raise TypeError("snapshot data must be an object")
    if "messages" in raw:
        fields["messages"] = deserialize_messages(raw["messages"])
    for key in ("state", "systemPrompt"):
        if key in raw:
            fields[key] = raw[key]
    return fields