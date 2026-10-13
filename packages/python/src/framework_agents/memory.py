"""Memory: append + window. Parity: src/memory/index.ts (post-fix).

Pinned semantics (the TS sweep found these the hard way):
- save() APPENDS (windows to max); the agent loop does CLEAR-then-SAVE, never
  plain save, or every run duplicates the transcript.
- FileMemory: corrupted files fail LOUDLY (MEMORY_CORRUPT) instead of wiping
  whatever is still on disk on the next save; missing file = fresh memory.
- tmp file per flush is unique — concurrent flushes can't race on rename.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from .errors import AgentError
from .types import ChatMessage, deserialize_messages, serialize_messages


class Memory(Protocol):
    async def load(self) -> list[ChatMessage]: ...
    async def save(self, messages: list[ChatMessage]) -> None: ...
    async def clear(self) -> None: ...


class InMemoryMemory:
    def __init__(self, max_messages: int = 50) -> None:
        self._messages: list[ChatMessage] = []
        self.max_messages = max_messages

    async def load(self) -> list[ChatMessage]:
        return list(self._messages)

    async def save(self, messages: list[ChatMessage]) -> None:
        self._messages = [*self._messages, *messages][-self.max_messages:]

    async def clear(self) -> None:
        self._messages = []


class FileMemory:
    """JSON-file backed memory: same windowing as in-memory, atomic writes."""

    _MAX_UTF16_UNITS = 2048

    def __init__(self, directory: str, max_messages: int = 50) -> None:
        self.directory = Path(directory)
        self.file = self.directory / "memory.json"
        self.max_messages = max_messages
        self._cache: list[ChatMessage] | None = None

    async def load(self) -> list[ChatMessage]:
        return list(await self._read_all())

    async def save(self, messages: list[ChatMessage]) -> None:
        all_messages = [*(await self._read_all()), *messages][-self.max_messages:]
        self._cache = all_messages
        await self._flush()

    async def clear(self) -> None:
        self._cache = []
        await self._flush()

    async def _read_all(self) -> list[ChatMessage]:
        if self._cache is not None:
            return self._cache
        try:
            raw = self.file.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._cache = []
            return self._cache
        except OSError as err:
            self._cache = []
            return self._cache
        try:
            data = json.loads(raw)
            # Valid JSON with the WRONG shape is still corrupt data. TS
            # throws here (deserializeMessages rejects non-arrays); silently
            # treating a dict as "empty" would clobber the file on save.
            if not isinstance(data, list):
                raise ValueError("top-level JSON is not a message array")
            self._cache = deserialize_messages(data)
        except (ValueError, TypeError) as err:
            # Corrupted file must FAIL loudly: returning [] would make the
            # next save overwrite whatever was still on disk.
            raise AgentError("MEMORY_CORRUPT", f"Memory file is not valid JSON for messages ({self.file}): {err}", retryable=False) from err
        return self._cache

    async def _flush(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        # Unique tmp per flush: concurrent saves sharing one fixed name race
        # on the rename and can publish a torn or half-written file.
        fd, tmp_path = tempfile.mkstemp(dir=self.directory, prefix="memory.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(serialize_messages(self._cache or []), fh)
            os.replace(tmp_path, self.file)
        finally:
            if os.path.exists(tmp_path):  # replace succeeded -> gone already
                os.unlink(tmp_path)