# Memory & sessions

Three layers of persistence, in both languages (TS names first, Python after the slash where they differ):

## Memory

`Memory` contract: `load()`, `save(messages)`, `clear()` (all async in both).

- **`InMemoryMemory(maxMessages = 50)`** — appends on `save`, keeping the newest `maxMessages` messages.
- **`FileMemory(dir, maxMessages = 50)`** — same windowing, persisted to `<dir>/memory.json`, write-then-rename to a unique temp file per flush (atomic publish; concurrent saves can't tear a file). A corrupted file fails **loudly** (`MEMORY_CORRUPT`), never returns `[]` — the next save would have wiped the disk data silently. A missing file means fresh memory.
- **`CompactedMemory(inner, { maxMessages = 40, summarize? })`** — wraps any memory; past the cap, the overflow head is summarized (pluggable `summarize`, default extractive stub) and replaced by a single boundary message.
- **`GraphMemory(store, inner, { facts })`** — session memory + long-term store in one: `load()` appends the top-`facts` (default 8) graph-closure entries relevant to the latest user turn as a grounding message; `save()` delegates to the inner memory; extraction of triples stays model-driven via `graph_add`, never silent parsing. `exportState()`/`importState()` serialize the closure (entailment quads dropped) for snapshots. See [semantic graph](semantic.md).

**Pinned semantics:** `save()` APPENDS, so at run end the agent loop does **clear-then-save** on the managed transcript — without the clear, every run re-saves the already-loaded history and duplicates the transcript.

## Conversation managers

`ConversationManager` keeps history inside context limits: `apply()` (after-run, before memory save), optional `beforeModel()` (per-turn inside the loop), `reduce()` (explicit shrink). Pinned messages are never removed, and dangling pairs are trimmed (`trimDangling` drops leading tool results / call-only assistant heads so the history never opens on an orphaned tool message).

- **`NullConversationManager`** — keeps everything (the TS loop default; pass another manager explicitly to manage).
- **`SlidingWindowConversationManager({ windowSize = 40, pinFirst = 0, perTurn = false })`** — keeps the newest `windowSize` unpinned messages; `pinFirst` permanently protects the first N. Kept messages merge in **ORIGINAL order** (hoisting pinned messages to the front would reorder the conversation).
- **`SummarizingConversationManager({ summaryRatio = 0.3, preserveRecent = 10, pinFirst, summarizer?, model? })`** — summarizes the oldest `ratio` fraction (clamped 0.1–0.8) into one assistant message (`summaryPrefix` settable; `lastSummary` exposed for asserts); summarization uses your `summarizer` fn, the passed `model`, or falls back to an extractive stub.

Python: `NullConversationManager`, `SlidingWindowConversationManager` / `SummarizingConversationManager` with the same knobs (`window_size`, `pin_first`, `per_turn`, `summary_ratio`, `preserve_recent`, `summarizer`, `model`).

## SessionManager + snapshots

`SessionManager({ sessionId, storage, agentId? })` persists state + history across restarts: restored on the first run (**before** `initialMessages` seeding — session wins over seed), saved after each run.

```ts
import { SessionManager, LocalFileStorage } from "framework-agents";

const session = new SessionManager({
  sessionId: "user-42",
  storage: new LocalFileStorage("./.data/sessions"),
});
const agent = new Agent({ model, sessionManager: session });
// First run: State + history restored from snapshot_latest.json, then saved after every run.
```

- Storage backends: `InMemoryStorage`, `LocalFileStorage(dir)` (byte-moving, traversal-safe — keys containing `..` are rejected).
- `saveLatest/loadLatest` — the latest snapshot at `session/<id>/scopes/agent/<id>/snapshots/snapshot_latest.json`.
- `saveSnapshot(data, appData?)` / `listSnapshotIds()` / `restoreSnapshot(id)` — immutable UUID-keyed checkpoints (agent-level `takeSnapshot(opts)` / `loadSnapshot(snapshot)` for manual ones).

Snapshot field selection (`TakeSnapshotOptions` **TS-tested semantics**, mirrored by `select_snapshot_fields`):

- `preset: "session"` → base bundle `{ messages, state }` (the only preset so far).
- `include: ("messages"|"state"|"systemPrompt")[]` adds on top.
- `exclude: SnapshotField[]` removes from the base.
- With no preset, include starts from the **empty** base (include-only selection).
- `appData` rides along untouched. `loadSnapshot` restores only fields present in the snapshot; absent fields keep current values.

Python: `SessionManager(session_id, storage, agent_id)`, `Snapshot`/`SnapshotData`, `InMemoryStorage`, `LocalFileStorage`, `save_latest/load_latest`, `save_snapshot/list_snapshot_ids/restore_snapshot`, `take_snapshot`, `load_snapshot` with the same preset/include/exclude rules.

## Pause/resume, checkpoints, and HITL

`FileCheckpointer(root)` stores `ThreadCheckpoint { threadId, savedAt, messages, state, pending }` under `<root>/threads/<threadId>/checkpoint.json`, guarded by `withFileLock` (O_EXCL + stale-lock reclaim; `threadId` must be a single safe directory name).

- **`agent.pause(threadId, { checkpointer })` / `agent.resume(threadId, decisions?, { checkpointer })`** — mid-conversation suspend without an interrupt; resume restores state + messages + memory and continues the loop with **no new user message**. `CHECKPOINT_MISSING` if unknown.
- **Checkpointed HITL** — when a run executes with `{ checkpointer, threadId }` (or A2A serving with a checkpointer, where `threadId = contextId`), a `HitlInterrupt` saves messages up to (and excluding) the assistant turn holding the interrupted call — matched by tool name + JSON args equality so a resume re-issues the call from prior state — plus the pending call itself.
- **Decision scope (the wildcard)** — stored decisions are keyed by `hitlKey(tool, args)` (`f"{tool}:{json.dumps(args)}"`), but **a bare tool-name key matches every re-issued call of that tool**. Models re-issue interrupted calls with regenerated argument text (temperature), so exact-key-only approval never converges; `{"publish": "approve"}` approves the re-issued call. This is a pinned semantic in both languages.

```python
try:
    await mk().run("...publish it.", checkpointer=cp, thread_id="t1")
except HitlInterrupt as first:
    # NOTE: Python DELETES the `as` binding when the except block ends.
    tool = first.tool   # stash what you need BEFORE leaving the block
resumed = await mk().resume("t1", {tool: "approve"}, checkpointer=cp)
```

Other decisions: `{ edit: args }` rewrites the arguments, `{ reject: "reason" }` throws `ToolRejection` (model-visible feedback, run continues), `{ suspend: true }` (the default when no `decide` callback) raises the interrupt. `HITL_DECISION` fires when a decision isn't in the tool's `allowedDecisions`.

## editMessage, branch, timeline

- **`editMessage(idOrIndex, content)` / `edit_message`** — edits history by message id or index and keeps memory in sync, recording an `edit` in the timeline. **Guard:** editing a message that contains tool calls throws `MESSAGE_NOT_EDITABLE` — the following `role:"tool"` messages reference its tool-call ids and providers 400 on unpaired results; `branch()` from a point before it instead. Unknown id/index → `MESSAGE_NOT_FOUND`.
- **`branch(label, overrides?)`** — forks the conversation into an independent agent: model, tools, middleware, and response format shared; state copied; memory fresh (seeded with the current history); sessions NOT carried over (avoids double-persist collisions). Both timelines record the branch.
- **`timeline`** — append-only `TimelineEntry { rev, kind: "run"|"edit"|"branch"|"resume"|"snapshot", label?, at }` — every run, edit, branch, resume, and snapshot lands there.
- `outputVersion` (`"v0"`/`"v1"`) — v0 stores assistant turns as plain text; v1 also stores thinking as `reasoning` blocks on final turns.
- `withTools(...extra)` clones for injected control tools (used by [swarm](multi-agent.md)) — memory, session, and state are fresh per clone.

## Related

- [Middleware & HITL](middleware.md) — `humanInTheLoop` middleware that raises the interrupts above
- [Multi-agent](multi-agent.md) — clones and coordination

> Frontend cross-section: how sessions/threads/state appear to a React (or any SSE) client — [Sessions, threads & state](sessions-and-state.md).
