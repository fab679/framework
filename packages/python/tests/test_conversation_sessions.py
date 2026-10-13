"""Tests ported from the TS suites: conversation managers, sessions,
checkpoints, compaction, and an agent end-to-end run."""

from __future__ import annotations

import time

import pytest

from framework_agents.agent import Agent, AgentOptions
from framework_agents.checkpoint import FileCheckpointer, PendingCall, ThreadCheckpoint, with_file_lock
from framework_agents.conversation import (
    NullConversationManager,
    SlidingWindowConversationManager,
    SummarizingConversationManager,
    estimate_tokens,
    history_tokens,
    trim_dangling,
)
from framework_agents.errors import AgentError
from framework_agents.memory import FileMemory, InMemoryMemory
from framework_agents.memory_compaction import CompactedMemory, default_summarize
from framework_agents.models import MockModelProvider
from framework_agents.sessions import (
    InMemoryStorage,
    LocalFileStorage,
    SessionManager,
    SnapshotData,
    select_snapshot_fields,
)
from framework_agents.types import ChatMessage, assistant, tool_result, user


def _users(n: int) -> list[ChatMessage]:
    return [user(f"message {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# Directional sanity on the token estimator (parity: estimateTokens).
# ---------------------------------------------------------------------------


def test_estimate_tokens_calibration() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1  # calibrated anchor from TS
    # CJK: each ideograph/kana is its own token.
    assert estimate_tokens("日本語") == 3
    # A single long alphabetic word costs ceil(len / 5).
    assert estimate_tokens("abcdefghij") == 2
    assert history_tokens(_users(3)) > 0


# ---------------------------------------------------------------------------
# Sliding window.
# ---------------------------------------------------------------------------


async def test_sliding_window_keeps_newest_and_pins_first_in_order() -> None:
    messages = _users(12)
    mgr = SlidingWindowConversationManager(window_size=4, pin_first=2)

    kept = await mgr.apply(messages)

    # Newest 4 (indices 8..11) plus the pinned first 2 — in ORIGINAL order,
    # pinned messages are not hoisted to the front.
    assert [m.content for m in kept] == [
        "message 0",
        "message 1",
        "message 8",
        "message 9",
        "message 10",
        "message 11",
    ]
    # 10 unpinned messages - 4 window = 6 removed.
    assert mgr.removed_count == 6
    # reduce() enforces the same window.
    kept2 = await mgr.reduce(kept)
    assert len(kept2) == 6  # pinned do not count against the window
    assert mgr.removed_count == 6


async def test_sliding_window_trim_drops_leading_orphans() -> None:
    orphan_tail = [
        tool_result("t2", "date", "Sun Oct 11 2026"),
        user("what day is it?"),
    ]
    mgr = SlidingWindowConversationManager(window_size=10)
    # A tool head lost its assistant tool_call pair -> dropped by trim_dangling.
    kept = await mgr.apply(orphan_tail)
    assert [m.content for m in kept] == ["what day is it?"]

    # Assistant head made only of tool_call blocks is dropped too — and its
    # now-pairless tool result follows it into the head and is dropped as
    # well (pairing is POSITION-based in TS: any leading dangling chain goes).
    head_only_calls = [
        assistant([{"type": "tool_call", "id": "t1", "name": "date"}]),
        tool_result("t1", "date", "Sun Oct 11 2026"),
        user("thanks"),
    ]
    kept2 = await mgr.apply(head_only_calls)
    assert [m.content for m in kept2] == ["thanks"]


def test_trim_dangling_honors_pinned() -> None:
    orphan = user("orphan", id="u1")
    orphan.pinned = True
    rest = [tool_result("t9", "date", "today"), user("hi")]
    kept = trim_dangling([orphan, *rest])
    # Pinned messages stop the scan even with a dangling message after them.
    assert kept[0] is orphan


async def test_null_manager_is_identity() -> None:
    mgr = NullConversationManager()
    messages = _users(3)
    assert await mgr.apply(messages) == messages
    assert await mgr.reduce(messages) == messages
    assert await mgr.before_model(messages, 0) == messages
    assert mgr.removed_count == 0
    assert mgr.name == "null"


# ---------------------------------------------------------------------------
# Summarizing manager.
# ---------------------------------------------------------------------------


async def test_summarizing_keeps_summary_and_recent_tail() -> None:
    messages = _users(20)
    calls: list[int] = []

    async def summarizer(head: list[ChatMessage]) -> str:
        calls.append(len(head))
        return f"summary-of-{len(head)}"

    mgr = SummarizingConversationManager(
        summary_ratio=0.5,
        preserve_recent=5,
        summarizer=summarizer,
        summary_prefix="Earlier:",
    )
    result = await mgr.apply(messages)

    # eligible = 20, count = min(20 - 5, floor(20 * 0.5)) = 10 summarized.
    assert calls == [10]
    assert mgr.last_summary == "summary-of-10"
    assert result[0].role == "assistant"
    assert result[0].content == "Earlier:\nsummary-of-10"
    # 1 summary message + 10-message tail: 10 head messages compressed into 1.
    assert mgr.removed_count == 9
    assert [m.content for m in result[1:]] == [f"message {i}" for i in range(10, 20)]


async def test_summarizing_noop_when_head_empty() -> None:
    mgr = SummarizingConversationManager(preserve_recent=50)
    messages = _users(5)
    result = await mgr.apply(messages)
    assert result == messages
    assert mgr.last_summary is None
    # reduce() never summarizes (TS parity: identity).
    assert await mgr.reduce(messages) == messages


async def test_summarizing_model_based_uses_model() -> None:
    from framework_agents.models import ModelCompletion, ScriptedModelProvider

    model = ScriptedModelProvider([ModelCompletion(text="condensed")])
    mgr = SummarizingConversationManager(
        summary_ratio=0.8,
        preserve_recent=1,
        model=model,
    )
    result = await mgr.apply(_users(10))
    assert result[0].content == "Summary of earlier conversation:\ncondensed"


# ---------------------------------------------------------------------------
# Compaction.
# ---------------------------------------------------------------------------


async def test_compacted_memory_compacts_on_save() -> None:
    inner = InMemoryMemory(max_messages=1000)
    mem = CompactedMemory(inner, max_messages=5)
    await mem.save(_users(9))
    stored = await mem.load()
    # 1 default summary + the newest 4 = exactly max_messages.
    assert len(stored) == 5
    assert stored[0].role == "assistant"
    assert stored[0].content.startswith("Prior context summary")
    assert default_summarize(_users(5)).startswith("Prior context summary")
    assert [m.content for m in stored[1:]] == [f"message {i}" for i in range(5, 9)]

    # Custom summarizer is used for the overflow.
    custom = CompactedMemory(inner, max_messages=3, summarize=lambda ms: "compact!")
    await custom.save(_users(4))
    stored2 = await inner.load()
    assert stored2[0].content == "compact!"
    assert len(stored2) == 3


# ---------------------------------------------------------------------------
# Snapshot field selection (TS-tested semantics).
# ---------------------------------------------------------------------------


def _full_snapshot() -> SnapshotData:
    return SnapshotData(messages=_users(2), state={"a": 1}, system_prompt="sys")


def test_select_include_only_picks_exactly_those_fields() -> None:
    # No preset -> base is EMPTY; include-only picks just messages.
    sel = select_snapshot_fields(_full_snapshot(), include=["messages"])
    assert sel.messages is not None
    assert sel.state is None
    assert sel.system_prompt is None


def test_select_session_preset_and_exclude() -> None:
    sel = select_snapshot_fields(_full_snapshot(), preset="session")
    assert sel.messages is not None
    assert sel.state is not None
    assert sel.system_prompt is None

    sel2 = select_snapshot_fields(_full_snapshot(), preset="session", exclude=["state"])
    assert sel2.messages is not None
    assert sel2.state is None

    sel3 = select_snapshot_fields(_full_snapshot(), include=["messages", "systemPrompt"], exclude=["messages"])
    assert sel3.messages is None
    assert sel3.system_prompt == "sys"


def test_select_empty_by_default() -> None:
    sel = select_snapshot_fields(_full_snapshot())
    assert sel.messages is None
    assert sel.state is None
    assert sel.system_prompt is None


# ---------------------------------------------------------------------------
# Storage.
# ---------------------------------------------------------------------------


async def test_local_file_storage_key_validation_rejects_traversal() -> None:
    storage = LocalFileStorage("/tmp/does-not-matter")
    with pytest.raises(AgentError) as err:
        await storage.read("../evil")
    assert err.value.code == "STORAGE_KEY_INVALID"
    with pytest.raises(AgentError):
        await storage.write("nested/../../evil", "x")
    with pytest.raises(AgentError):
        await storage.delete_prefix("../evil")
    # Null bytes are path separators on some platforms: reject loudly.
    with pytest.raises(AgentError):
        await storage.read("evil\0.txt")


async def test_local_file_storage_roundtrip_and_list(tmp_path) -> None:
    storage = LocalFileStorage(str(tmp_path / "root"))
    # Trailing/duplicate slashes are harmless.
    await storage.write("a///b//key.json", "value")
    assert await storage.read("a/b/key.json") == "value"
    assert await storage.list("a/") == ["a/b/key.json"]
    assert await storage.list("missing/") == []
    await storage.delete_prefix("a/")
    assert await storage.read("a/b/key.json") is None


async def test_in_memory_storage() -> None:
    storage = InMemoryStorage()
    assert await storage.read("k") is None
    await storage.write("k", "v")
    await storage.write("k2", "v2")
    assert await storage.list("k") == ["k", "k2"]
    await storage.delete_prefix("k")
    assert await storage.list("") == []


# ---------------------------------------------------------------------------
# SessionManager.
# ---------------------------------------------------------------------------


async def test_session_manager_latest_and_immutable_snapshots() -> None:
    storage = InMemoryStorage()
    mgr = SessionManager(storage, session_id="s1", agent_id="bot")
    assert mgr.scope() == "session/s1/scopes/agent/bot/snapshots"

    assert await mgr.load_latest() is None
    await mgr.save_latest(SnapshotData(messages=_users(1), state={"n": 1}))
    latest = await mgr.load_latest()
    assert latest is not None
    assert latest.messages[0].content == "message 0"
    assert latest.state == {"n": 1}

    id1 = await mgr.save_snapshot(select_snapshot_fields(_full_snapshot(), preset="session"), app_data={"tag": "v1"})
    id2 = await mgr.save_snapshot(SnapshotData(state={"n": 2}))
    ids = await mgr.list_snapshot_ids()
    assert sorted([id1, id2]) == ids

    restored = await mgr.restore_snapshot(id1)
    assert restored is not None
    assert restored.app_data == {"tag": "v1"}
    assert [m.content for m in restored.data.messages or []] == ["message 0", "message 1"]
    assert await mgr.restore_snapshot("nope") is None

    # Corrupt JSON in latest degrades to None, not an exception.
    await storage.write(f"{mgr.scope()}/snapshot_latest.json", "{not json")
    assert await mgr.load_latest() is None

    await mgr.delete_session()
    assert await storage.list("session/s1/") == []


async def test_session_manager_delete_session(tmp_path) -> None:
    storage = LocalFileStorage(str(tmp_path))
    mgr = SessionManager(storage, session_id="sx")
    await mgr.save_latest(SnapshotData(state={"ok": True}))
    await mgr.delete_session()
    assert await mgr.load_latest() is None


# ---------------------------------------------------------------------------
# Checkpoints.
# ---------------------------------------------------------------------------


async def test_checkpoint_save_load_roundtrip(tmp_path) -> None:
    cp = FileCheckpointer(str(tmp_path))
    payload = ThreadCheckpoint(  # thread_id/saved_at are overwritten on save
        thread_id="ignored",
        saved_at="ignored",
        messages=[user("hello"), assistant("hi there")],
        state={"count": 3, "nested": {"deep": True}},
        pending=[PendingCall(interrupt_id="i1", tool="confirm", args={"x": 1})],
    )

    await cp.save("t1", payload)
    loaded = await cp.load("t1")
    assert loaded is not None
    assert loaded.thread_id == "t1"
    assert loaded.saved_at != "ignored"
    assert [m.content for m in loaded.messages] == ["hello", "hi there"]
    assert loaded.state["count"] == 3
    assert loaded.pending[0].tool == "confirm"
    assert loaded.pending[0].args == {"x": 1}

    await cp.delete("t1")
    assert await cp.load("t1") is None
    await cp.delete("t1")  # deleting a missing checkpoint is not an error

    # Missing thread loads as None; corrupted files degrade to None too.
    assert await cp.load("never") is None
    target = tmp_path / "threads" / "t2" / "checkpoint.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{corrupt", encoding="utf-8")
    assert await cp.load("t2") is None


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b"])
async def test_checkpoint_rejects_unsafe_thread_ids(tmp_path, bad: str) -> None:
    cp = FileCheckpointer(str(tmp_path))
    payload = ThreadCheckpoint(thread_id="x", saved_at="x")
    with pytest.raises(AgentError) as err:
        await cp.save(bad, payload)
    assert err.value.code == "THREAD_ID_INVALID"
    with pytest.raises(AgentError):
        await cp.load(bad)
    with pytest.raises(AgentError):
        await cp.delete(bad)


async def test_with_file_lock_exclusive_and_stale_reclaim(tmp_path) -> None:
    os = __import__("os")
    Path = __import__("pathlib").Path
    lock = str(tmp_path / "dir" / ".lock")

    async def inner() -> str:
        # While held, a second taker refuses a fresh lock.
        with pytest.raises(RuntimeError, match="Lock held"):
            await with_file_lock(lock, inner2, stale_ms=60_000)
        return "done"

    async def inner2() -> str:
        return ""

    assert await with_file_lock(lock, inner) == "done"
    # finally-unlink: the lock is gone after the body ran.
    assert not os.path.exists(lock)

    # A stale lock is reclaimed instead of blocking forever.
    lock2 = str(tmp_path / "stale" / ".lock")
    Path(lock2).parent.mkdir(parents=True, exist_ok=True)
    Path(lock2).write_text("")
    old = time.time() - 60
    os.utime(lock2, (old, old))
    marker: list[str] = []

    async def body() -> str:
        marker.append("ran")
        return "ok"

    assert await with_file_lock(lock2, body, stale_ms=1_000) == "ok"
    assert marker == ["ran"]


# ---------------------------------------------------------------------------
# End to end: agent + memory (no duplicated history across runs).
# ---------------------------------------------------------------------------


async def test_agent_two_runs_do_not_duplicate_history(tmp_path) -> None:
    memory = FileMemory(str(tmp_path / "mem"))
    model = MockModelProvider()
    agent = Agent(AgentOptions(model=model, memory=memory, name="e2e"))

    result1 = await agent.run("first question")
    stored1 = await memory.load()
    assert [m.content for m in stored1] == ["first question", result1.text]

    result2 = await agent.run("second question")
    stored2 = await memory.load()
    # Clear-then-save: the second run APPENDS one turn, never a duplicate.
    assert [m.content for m in stored2] == [
        "first question",
        result1.text,
        "second question",
        result2.text,
    ]
    for a, b in zip(stored2, stored2[1:]):  # no identical adjacent pair
        assert (a.role, a.content) != (b.role, b.content)

    # A conversation manager runs over the same persisted history safely:
    # sliding window keeps the newest turn and pins the opening question.
    mgr = SlidingWindowConversationManager(window_size=2, pin_first=1)
    managed = await mgr.apply([*stored2])
    assert [m.content for m in managed] == [
        "first question",
        "second question",
        result2.text,
    ]
    assert mgr.removed_count == 1


# small helper used above (SessionManager exposes no list on storage)
async def _nope() -> None: ...