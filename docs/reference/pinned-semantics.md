# Pinned semantics

The framework has two implementations of the same API — TypeScript
(`framework-agents` on npm, `packages/typescript`) and Python
(`framework-agents` on PyPI, `packages/python`). TypeScript is the reference
implementation; the Python port targets API parity with the **fixed** TS
semantics.

Most of the rules below were not design preferences. They are regression
pins: each one encodes the behavior the TypeScript line **after** a real bug
(or a wire-contract ambiguity) proved which behavior was correct. They are
recorded so that both implementations — and anything that speaks to either
over the wire — agree forever. The table is normative in both languages; the
sections after it explain where each rule came from.

| Pinned semantic | Why |
|---|---|
| `reasoning_tokens` is a SUBSET of `output_tokens` | never double-billed in budgets/cost |
| `cost_usd` only when a price is known | unknown models → `None`, never a fake $0 |
| `save()` APPENDS; the loop does clear-then-save | history never duplicates across runs |
| Structured `'auto'` resolves by capability | unresolved auto never throws STRUCTURED_NO_MATCH |
| `finish_reason=length` + zero content → loud `MODEL_ERROR` | a thinking model burning its whole budget is not a silent empty answer |
| Tool parameter schemas ride the wire | models see real argument shapes |
| HITL decisions: exact key THEN bare tool name | resume converges even when models regenerate arg text |
| Local `ollama:` pricing wins over name collisions | never bill cloud prices for free local models |
| Storage keys/threadIds reject traversal (`..`) | no writes outside the root |
| Remote SPARQL: descriptive UA + 429/5xx retry honoring Retry-After | public endpoints reject and rate-limit otherwise |

Naming note: TypeScript spells these fields `reasoningTokens`,
`outputTokens`, `costUsd`, `finishReason`; Python spells them
`reasoning_tokens`, `output_tokens`, `cost_usd`, `finish_reason`. The rules
are identical; only the casing differs.

## Why each pin exists

### 1. Reasoning tokens are a subset of output tokens

Providers report `reasoning_tokens` (thinking budget spend) *inside* the
`output_tokens` total, not on top of it. Budget checks and cost math used to
add the two fields together, which double-billed every thinking model — a run
could blow the output-token budget (or overstate cost) by counting the same
tokens twice. Now `TokenUsage.reasoningTokens` / `reasoning_tokens` is a
reporting-only field: budgets and cost always operate on `output_tokens`.
Both implementations compute budgets on **cumulative** usage, checked after
**every** model call.

### 2. Cost only when a price is known

Everyone's provider sends token counts; nobody sends prices. Cost is
computed client-side against a local price table, and an unknown model used
to fall back to `$0` — a number that looks authoritative and is silently
wrong. `usage.costUsd` / `cost_usd` is set **only** when a price for the
model exists; for unknown models it is `None`/`undefined`, never a fake
zero. Check it against your provider bill; the table is a ladder, not
ground truth.

### 3. `save()` appends; the loop does clear-then-save

The `Memory` interface appends on `save()`. The agent loop therefore must
`clear()` before its end-of-run `save()` — an early version saved without
clearing, so every run duplicated the previous transcript and history grew
quadratically. The split is pinned: `save()` stays append (so callers can
use Memory directly), and the loop's clear-then-save sequence is part of the
contract, not an implementation detail.

### 4. Structured `'auto'` resolves by capability

`responseFormat: 'auto'` means "pick a strategy for me". It resolves **by
provider capability** against the model's `capabilities`: `provider` when the
model advertises `structured_output`, the tool-based strategy otherwise. An
unresolved `'auto'` used to fall through as "no strategy", and every run
threw `STRUCTURED_NO_MATCH`. The resolution is deterministic per model, so
the failure mode is gone entirely — `'auto'` always has a strategy.

### 5. Truncation with zero content is a loud `MODEL_ERROR`

A thinking model that spends its entire `max_tokens` on an unfinished
reasoning trace used to come back as (what looked like) a successful, empty
answer — the worst failure mode: silent and cheap-looking. Now
`finish_reason == "length"` **combined with zero content and no tool calls**
raises a loud `MODEL_ERROR` with the fix spelled out (raise `max_tokens`);
partial content keeps flowing and merely carries the `finish_reason`
attached.

### 6. Tool parameter schemas ride the wire

Tool definitions were sent with only names and descriptions, so models
guessed argument shapes and emitted structurally invalid calls. The JSON
Schema derived from each tool's validation schema (zod in TS, pydantic in
Python — `function.parameters` on OpenAI-compatible wire forms) is sent with
every request. Models see the real argument shapes.

### 7. HITL decisions: exact key, then bare tool name

Human-in-the-loop stores approval decisions in the invocation state under
`hitl_key(tool, args)` — a key derived from the tool name plus its
arguments. On
resume, a model frequently **regenerates the argument text** (spacing,
ordering, quoting), so the exact key never matches and every resumed run
interrupted again — HITL could never converge. Resume now looks up the exact
`hitl_key(tool, args)` first, then falls back to the **bare tool name** as a
wildcard. The fallback is deliberate: a decision you already gave for a tool
applies to the tool.

### 8. Local `ollama:` pricing wins over name collisions

`qwen2.5` served by a local Ollama instance has the *same name* as a
cloud-hosted model with a real price per token. Name-matching used to pick
the cloud row, so free local inference was billed at cloud rates. Price
lookup checks the local/free table first; local `ollama:` models resolve to
free before cloud-name collisions are consulted.

### 9. Storage keys and threadIds reject path traversal

Session storage keys and checkpoint threadIds become filesystem paths. A key
like `"../.env"` (or a threadId containing `/`) used to escape the storage
root and write (or read) outside it. Keys containing `..`, and threadIds
that are not a single clean directory name (`""`, `.`, `..`, or anything with
`/`), now raise a non-retryable `STORAGE_KEY_INVALID` /
`THREAD_ID_INVALID` before anything touches disk.

### 10. Remote SPARQL: descriptive UA and Retry-After-aware retries

A remote SPARQL adapter is only useful if real public endpoints (Wikidata's
query service notably) actually answer it — and they reject requests without
a descriptive `User-Agent` with `403`, then rate-limit aggressively (frequent
`429` responses, often with empty bodies). The adapter ships a descriptive
UA by default and retries `429`/`5xx`/network errors, honoring the
`Retry-After` header (exponential backoff, capped) when present.

## Where the bodies are buried

Each pin is enforced by tests in both trees: the TypeScript regression suites
in `packages/typescript/tests/` and the Python offline parity suite in
`packages/python/tests/` (run together with the cross-language conformance
suites; see [Cross-language parity](parity.md)). If you think a pin is wrong,
change it in **both** languages in the same commit — or the cross-language
wire tests will find you.