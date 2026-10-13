"""State in three lifetimes. Parity: src/state/index.ts.

- Conversation history: messages (model-visible), via Memory.
- Agent state: validated JSON key-value store, NEVER sent to the model.
- Invocation state: per-run data, shared by reference across hooks and
  tools within one run, returned on the result.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

_PLAIN_SCALARS = (str, int, float, bool)


def assert_json_serializable(key: str, value: Any) -> None:
    """Reject anything that would not survive a toJSON/fromJSON round-trip.

    Pinned from the TS assertJsonSerializable: functions/symbols/bigints via
    JSON-serialization failure, Map/Set-like containers, and class instances
    with a custom prototype (JSON.stringify silently turns them into
    {}/strings — reject at set() instead of corrupting later snapshots).
    """
    if callable(value):
        raise ValueError(f'AgentState: value for "{key}" is not JSON-serializable')
    # Set-like containers serialize into {} / crash — reject with a targeted
    # message instead of a corrupt value in every later snapshot/restore.
    if isinstance(value, (set, frozenset, bytearray, memoryview)):
        raise ValueError(
            f'AgentState: value for "{key}" is a {type(value).__name__}; use a plain object/array'
        )
    if isinstance(value, dict) and type(value) is not dict:
        raise ValueError(
            f'AgentState: value for "{key}" has a custom prototype; plain objects round-trip, class instances don\'t'
        )
    if isinstance(value, list) and type(value) is not list:
        raise ValueError(
            f'AgentState: value for "{key}" has a custom prototype; plain objects round-trip, class instances don\'t'
        )
    if value is not None and not isinstance(value, _PLAIN_SCALARS + (dict, list)):
        raise ValueError(
            f'AgentState: value for "{key}" has a custom prototype; plain objects round-trip, class instances don\'t'
        )
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        raise ValueError(f'AgentState: value for "{key}" is not JSON-serializable') from None


class AgentState:
    """Key-value store outside the model context. Tools + app logic read/write freely."""

    def __init__(self, initial: Optional[dict[str, Any]] = None) -> None:
        data = dict(initial or {})
        for key, value in data.items():
            assert_json_serializable(key, value)
        self._data: dict[str, Any] = data

    def get(self, key: Optional[str] = None) -> Union[Any, dict[str, Any]]:
        if key is None:
            return dict(self._data)
        return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        assert_json_serializable(key, value)
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def to_json(self) -> dict[str, Any]:
        """Plain-object copy for snapshots/persistence (deep round-trip).

        Values mutated by reference after set() are still validated here: an
        unserializable nested value surfaces as a ValueError naming the key
        that leaked it, not a raw TypeError from json.dumps."""
        for key, value in self._data.items():
            try:
                json.dumps(value)
            except (TypeError, ValueError) as err:
                raise ValueError(
                    f"AgentState: use plain objects/lists; value under {key!r} is not JSON-serializable"
                ) from err
        return json.loads(json.dumps(self._data))

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "AgentState":
        return cls(raw)