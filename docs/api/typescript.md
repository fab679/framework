# TypeScript API reference (`framework-agents`)

Every public symbol of the TypeScript package, grouped exactly as it is
exported from the barrel (`packages/typescript/src/index.ts`). Import from the
package root — never from deep paths:

```ts
import { Agent, defineTool, initChatModel } from "framework-agents";
```

Signatures below are copied from the source; descriptions summarize the code's
own doc comments. The shared error contract (codes, retryable verdicts) is on
[Errors & retry contract](errors.md).

---

## Types (`types/`)

### `ContentBlock`

Standard, provider-neutral message payload. `content` stays
`string | ContentBlock[]`: strings for simple cases, blocks for multimodal /
reasoning / tool-call payloads.

```ts
type ContentBlock =
  | { type: "text"; text: string }
  | { type: "reasoning"; reasoning: string }
  | { type: "image"; url?: string; data?: string; mimeType?: string; fileId?: string }
  | { type: "audio"; url?: string; data?: string; mimeType?: string; fileId?: string }
  | { type: "file"; url?: string; data?: string; mimeType?: string; fileId?: string }
  | { type: "tool_call"; id: string; name: string; args: unknown }
  | { type: "non_standard"; value: unknown };
```

### `textOf` / `wireTextOf`

```ts
function textOf(content: string | ContentBlock[]): string;
function wireTextOf(content: string | ContentBlock[]): string;
```

- `textOf` — plain-text view of any content shape (what the model "reads").
- `wireTextOf` — text for re-sending history to a provider: reasoning blocks are
  dropped, because a model's own past thinking must not come back as ordinary
  assistant text (duplicated context, and Anthropic rejects thinking-as-text).

### `hasImages` / `imagesOf`

```ts
function hasImages(content: string | ContentBlock[]): boolean;
function imagesOf(content: string | ContentBlock[]):
  { url?: string; data?: string; mimeType?: string }[];
```

`imagesOf` returns images in wire-ready form (url or base64 data).

### `Brand`, `AgentId`, `RunId`, `ToolName`

```ts
type Brand<T, B extends string> = T & { readonly __brand: B };
type AgentId = Brand<string, "AgentId">;
type RunId = Brand<string, "RunId">;
type ToolName = Brand<string, "ToolName">;
```

Nominal string types for stable ids.

### `ChatMessage`

```ts
interface ChatMessage {
  id?: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string | ContentBlock[];
  name?: string;        // provider user-identification; behavior varies
  toolCallId?: string;
  toolName?: string;
  pinned?: boolean;     // pinned messages survive conversation-manager trimming
}
```

### `AgentResult`

```ts
interface AgentResult {
  text: string;
  messages: ChatMessage[];
  toolCalls: ExecutedToolCall[];
  usage: TokenUsage;
  structuredResponse?: unknown;             // set when run with responseFormat
  invocationState?: Record<string, unknown>; // same reference mutated during the run
}
```

### `ExecutedToolCall` / `TokenUsage`

```ts
interface ExecutedToolCall {
  id: string;
  name: string;
  args: unknown;
  output: unknown;
  artifact?: unknown; // downstream-only data (never sent to the model)
}

interface TokenUsage {
  inputTokens: number;
  outputTokens: number;
  reasoningTokens?: number; // SUBSET of outputTokens on every supported provider
  costUsd?: number;         // USD cost when a price is known — never guessed
}
```

### `Result`

```ts
type Result<T, E = AgentError> = { ok: true; value: T } | { ok: false; error: E };
```

### `AgentError`

```ts
class AgentError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  constructor(code: string, message: string, retryable = false);
}
```

Every raise carries a stable `code` and a `retryable` verdict. See
[Errors & retry contract](errors.md) for the full code table.

### Message factories: `system`, `user`, `assistant`, `toolResult`

```ts
function system(content: string | ContentBlock[]): ChatMessage;
function user(content: string | ContentBlock[], opts?: { id?: string; name?: string }): ChatMessage;
function assistant(content: string | ContentBlock[]): ChatMessage;
function toolResult(toolCallId: string, toolName: string, content: string | ContentBlock[]): ChatMessage;
```

Factory helpers with stable ids (`msg_1`, `msg_2`, ...) — no class hierarchy.

### `serializeMessages` / `deserializeMessages`

```ts
function serializeMessages(messages: ChatMessage[]): unknown[];
function deserializeMessages(raw: unknown[]): ChatMessage;
```

Serialize history for persistence (plain JSON-safe objects); deserialize +
validate history (e.g. resume from disk). Throws on bad shape.

---

## Config (`config/`)

### `FrameworkConfig`

```ts
type FrameworkConfig = {
  TOGETHER_API_KEY: string; TOGETHER_BASE_URL: string; TOGETHER_MODEL: string;
  OPENAI_API_KEY: string; OPENAI_BASE_URL: string; OPENAI_MODEL: string;
  ANTHROPIC_API_KEY: string; ANTHROPIC_BASE_URL: string; ANTHROPIC_MODEL: string;
  OLLAMA_BASE_URL: string; OLLAMA_MODEL: string;
  E2E_MODEL: string;
  active: { baseUrl: string; apiKey: string; model: string; source: "together" | "openai" };
};
```

`active` holds the resolved provider settings — TogetherAI wins when its key is
set. Defaults: `TOGETHER_BASE_URL https://api.together.xyz/v1`,
`TOGETHER_MODEL Qwen/Qwen3.5-9B`, `OPENAI_BASE_URL https://api.openai.com/v1`,
`OPENAI_MODEL gpt-4o-mini`, `ANTHROPIC_BASE_URL https://api.anthropic.com`,
`ANTHROPIC_MODEL claude-sonnet-4-6`, `OLLAMA_BASE_URL http://localhost:11434/v1`.

### `loadConfig`

```ts
function loadConfig(env: NodeJS.ProcessEnv = process.env): FrameworkConfig;
```

Loads `.env` from cwd, then walks up three parents (FRAMEWORK_ENV_PATH overrides
everything; every step is silent when the file is missing). Result is cached.

### `__resetConfig`

```ts
function __resetConfig(): void; // test-only: clear the cached config
```

---

## Agent (`agent/`)

### `AgentOptions`

```ts
type OutputVersion = "v0" | "v1";

interface AgentOptions {
  name?: string;
  /** One-line specialty, used by the router menu. */
  description?: string;
  systemPrompt?: string;
  model: ModelProvider;
  tools?: Tool[];
  memory?: Memory;
  maxSteps?: number;                    // default 6
  /** Max tool executions per run; exceeded -> AgentError(BUDGET_EXCEEDED). Default: unlimited. */
  maxToolCalls?: number;
  /** Token/cost/deadline budgets enforced after every model call. */
  budgets?: TokenBudgets;
  /** v0: assistant turns carry plain text. v1: thinking also stored as reasoning blocks. */
  outputVersion?: OutputVersion;
  /** Typed final answer (tool or provider strategy). Sets structuredResponse on the result. */
  responseFormat?: ResponseFormat;
  /** Middleware hooks (before/after model + tool). Run in order. */
  middleware?: AgentMiddleware[];
  /** Conversation history: seed prior messages to continue a conversation. */
  initialMessages?: ChatMessage[];
  /** Agent key-value state (never sent to the model). */
  state?: Record<string, unknown> | AgentState;
  /** History management. Default: NullConversationManager (keep everything; memory still windows). */
  conversationManager?: ConversationManager;
  /** Persist state + history across restarts. Restored on first run, saved after each run. */
  sessionManager?: SessionManager;
  events?: AgentEvents;
}
```

Middleware-contributed tools are prepended and the whole list is deduped by
name (first occurrence wins).

### `TokenBudgets`

```ts
interface TokenBudgets {
  /** Total (input + output) tokens per run — reasoning is a subset, never added on top. */
  maxTokens?: number;
  /** USD cap per run. Needs a known price (table lookup or prices) — never guessed. */
  maxCostUsd?: number;
  /** Price override when the model is missing from the pricing table. */
  prices?: ModelPrice;
  /** Wall-clock deadline per run. */
  deadlineMs?: number;
}
```

### `RunOptions`

```ts
interface RunOptions {
  signal?: AbortSignal;
  /** Per-run data: shared by reference across hooks/tools, returned on the result, never sent to the model. */
  invocationState?: InvocationState;
  /** Checkpointed HITL: persist + resume human approvals locally. */
  checkpointer?: Checkpointer;
  threadId?: string;
}
```

### `Agent`

Minimal ReAct-style loop: load memory -> messages; loop `model.complete` ->
tool calls -> execute -> append; save memory, return `AgentResult`. `run()`
drains `stream()`; both fire `events` hooks identically.

```ts
class Agent {
  constructor(options: AgentOptions);

  get name(): string;
  get description(): string;
  get model(): ModelProvider;
  /** Member tools (copy) — introspection for orchestration/menus. */
  get tools(): Tool[];
  /** Key-value state (never sent to the model). */
  get state(): AgentState;
  /** Session handle: the manager's id, or a generated per-instance id. */
  get sessionId(): string;
  /** Full conversation history from the last run (system + memory + new turns). */
  get messages(): ChatMessage[];
  /** Version timeline: runs, edits, branches, resumes, snapshots (append-only). */
  get timeline(): TimelineEntry[];

  /** Clone with extra tools. Memory/session/state are NOT carried over; model,
      tools, middleware, and formats are shared. Used by coordinators that
      inject control tools (e.g. swarm handoff). */
  withTools(...extra: Tool[]): Agent;

  /** Edit a message by id or index and record the version in the timeline.
      Messages containing tool calls are not editable (branch before them). */
  async editMessage(idOrIndex: string | number, content: string | ContentBlock[]): Promise<ChatMessage>;

  /** Branch the current history into an independent agent. Model/tools/
      middleware/formats shared; state copied; memory fresh (seeded with this
      history); sessions NOT carried over. Both timelines record the branch. */
  async branch(label: string, overrides?: Partial<AgentOptions>): Promise<Agent>;

  /** Pause mid-conversation: persist history + state under threadId without an
      interrupt. Pair with resume() (empty decisions continue). */
  async pause(threadId: string, opts: { checkpointer: Checkpointer }):
    Promise<{ threadId: string; messages: number }>;

  /** Manual checkpoint: messages + state (+ system prompt on request) + appData. */
  takeSnapshot(opts?: TakeSnapshotOptions): Snapshot;

  /** Restore fields present in the snapshot; absent fields keep current values. */
  async loadSnapshot(snapshot: Snapshot): Promise<void>;

  /** Resume a suspended run: restore checkpoint, seed human decisions (keyed
      by hitlKey(tool, args), or by bare tool name for re-issued calls), and
      continue the loop without new input. */
  async resume(
    threadId: string,
    decisions?: Record<string, HitlDecision>,
    opts?: { checkpointer: Checkpointer } & RunOptions,
  ): Promise<AgentResult;

  run(input: string | ContentBlock[], signal?: AbortSignal): Promise<AgentResult>;
  run(input: string | ContentBlock[], opts?: RunOptions): Promise<AgentResult>;
  stream(input: string | ContentBlock[], signal?: AbortSignal): AsyncGenerator<StreamEvent, AgentResult>;
  stream(input: string | ContentBlock[], opts?: RunOptions): AsyncGenerator<StreamEvent, AgentResult>;
}
```

### `TimelineEntry`

```ts
interface TimelineEntry {
  rev: number;
  kind: "run" | "edit" | "branch" | "resume" | "snapshot";
  label?: string;
  at: string;
}
```

Behavior notes pinned by the loop:

- Unknown tools become model-visible feedback (`Unknown tool: ...`); the run continues.
- Structured `auto` resolves by provider capability (`structuredOutput`).
- `maxSteps` exhaustion stores a synthesized assistant message from tool outputs — history never ends on a dangling tool tail.
- Session restore wins over `initialMessages` on the first run.
- Memory save at run end is clear-then-save (`save()` appends).

---

## State (`state/`)

### `InvocationState` / `ToolContext`

```ts
type InvocationState = Record<string, unknown>;

interface ToolContext {
  agentName: string;
  state: AgentState;
  invocationState: InvocationState;
}
```

Per-run data + the agent state, shared by reference with hooks and tools within
one run; never sent to the model.

### `AgentState`

```ts
class AgentState {
  constructor(initial?: Record<string, unknown>);
  get<T = unknown>(key?: string): T | Record<string, unknown> | undefined;
  set(key: string, value: unknown): void;
  delete(key: string): void;
  /** Plain-object copy for snapshots/persistence. */
  toJSON(): Record<string, unknown>;
  static fromJSON(raw: Record<string, unknown>): AgentState;
}
```

Key-value store outside the model context. `set()` rejects non-JSON-serializable
values, `Map`/`Set`-like containers, and class instances (they would corrupt
every later snapshot/restore).

---

## Tools (`tools/`)

### `Tool` / `ToolResult` / `defaultToResult`

```ts
interface ToolResult {
  /** Model-visible content (string or blocks). */
  content: string | ContentBlock[];
  /** Downstream-only data — stored on the call record, never sent to the model. */
  artifact?: unknown;
}

interface Tool<TArgs = any, TOutput = any> {
  name: string;
  description: string;
  /** Any zod version (v3/v4) or anything with .parse — registry validates. */
  schema: { parse(input: unknown): unknown };
  /** Optional second arg receives ToolContext (state + invocationState). */
  execute: (args: TArgs, ctx?: ToolContext) => Promise<TOutput>;
  /** Split raw output into model-visible content + downstream artifact. */
  toResult?: (output: TOutput) => ToolResult;
}

function defaultToResult(output: unknown): ToolResult;
```

Default `toResult`: JSON/string content, no artifact. Override it to keep raw
payloads (retrieval metadata, debug blobs) out of the model's context.

### `defineTool`

```ts
type AnySchema = { parse(input: unknown): unknown };

interface ToolSpec<TArgs = any, TOutput = unknown> {
  name: string;
  description: string;
  schema: AnySchema;                                  // zod v3 / v4 / anything with .parse
  execute: (args: TArgs, ctx?: ToolContext) => Promise<TOutput>;
  toResult?: (output: TOutput) => ToolResult;
}

function defineTool<TArgs = any, TOutput = unknown>(tool: ToolSpec<TArgs, TOutput>): Tool<TArgs, TOutput>;
```

Helper that throws when `name` is missing. **TArgs is inferred from the
`execute` annotation** (annotate the destructure once — that is the parsed
shape), and `schema` accepts any zod version (v3, v4, or anything with
`.parse`) so consumer projects on different zod majors work. `import { z }
from "framework-agents"` re-exports the bundled instance if you want one zod
everywhere.

### `ToolRegistry`

```ts
class ToolRegistry {
  register(tool: Tool): this;
  get(name: string): Tool | undefined;
  list(): Tool[];
  async call<T>(name: string, rawArgs: unknown): Promise<T>;
}
```

Validating registry — the single place tools are looked up + arg-checked.
`call` raises `UNKNOWN_TOOL` / `TOOL_ARG_INVALID` (`AgentError`, not retryable).

The agent sends each tool's `parametersSchema` (JSON Schema via
`zod-to-json-schema`) with the model request — descriptions alone leave arg
shapes to guesswork.

---

## Streaming (`streaming/`)

### `StreamEvent`

```ts
type StreamEvent =
  | { type: "step"; step: number }
  | { type: "token"; delta: string }
  | { type: "thinking"; delta: string }
  | { type: "tool_call"; name: string; args: unknown }
  | { type: "tool_result"; name: string; output: unknown }
  | { type: "done"; text: string };
```

Typed run events with independent projections. Consume via
`for await (const e of agent.stream(input))`.

### `collectStream`

```ts
function collectStream(stream: AsyncGenerator<StreamEvent, AgentResult>):
  Promise<{ events: StreamEvent[]; result: AgentResult }>;
```

Collect a stream into buckets — handy for tests and simple UIs.

---

## Structured output (`structured/`)

### `ResponseFormat`

```ts
type ResponseStrategy = "auto" | "provider" | "tool";

interface ResponseFormat<T = unknown> {
  schema: z.ZodType<T>;
  /** Function/tool name. Default: "structured_response". */
  name?: string;
  strategy?: ResponseStrategy;
  /** Tool message content on success (tool strategy). */
  toolMessageContent?: string;
  /** Validation retries before STRUCTURED_NO_MATCH (tool strategy). Default 2. */
  maxAttempts?: number;
}
```

- `provider` — native `response_format` (OpenAI-compatible APIs). Most reliable.
- `tool` — synthetic `__structured_response` tool; validation errors return as
  tool messages so the model self-repairs within `maxSteps`.
- `auto` — provider capability decides (`capabilities.structuredOutput`).

### `toolStrategy` / `providerStrategy`

```ts
function toolStrategy<T>(schema: z.ZodType<T>, opts?: Omit<ResponseFormat<T>, "schema" | "strategy">): ResponseFormat<T>;
function providerStrategy<T>(schema: z.ZodType<T>, opts?: Omit<ResponseFormat<T>, "schema" | "strategy">): ResponseFormat<T>;
```

### `responseFormatName` / `responseFormatJsonSchema` / `parseStructured`

```ts
function responseFormatName<T>(format: ResponseFormat<T>): string;
function responseFormatJsonSchema<T>(format: ResponseFormat<T>): Record<string, unknown>;
function parseStructured<T>(format: ResponseFormat<T>, raw: unknown): T;
```

`parseStructured` validates raw model output; throws
`AgentError(STRUCTURED_INVALID, retryable=true)` on mismatch.

---

## Models (`models/`)

### `ModelProvider` / `CompleteOptions` / `ModelCompletion`

```ts
interface ModelProvider {
  readonly id: string;
  readonly capabilities?: { structuredOutput?: boolean; vision?: boolean; streaming?: boolean };
  complete(options: CompleteOptions): Promise<ModelCompletion>;
}

interface CompleteOptions {
  messages: ChatMessage[];
  /** Tool metadata; parametersSchema (JSON Schema) lets models call with real args. */
  tools: { name: string; description: string; parametersSchema?: Record<string, unknown> }[];
  /** Native structured output (OpenAI response_format). Honored when supported. */
  responseFormat?: ResponseFormatWire;
  /** Streamed text deltas (non-thinking). */
  onToken?: (delta: string) => void;
  /** Streamed thinking/reasoning deltas (reasoning_content). */
  onThinking?: (delta: string) => void;
  /** Aborts the in-flight provider request (fetch). */
  signal?: AbortSignal;
}

interface ModelCompletion {
  text: string;
  thinking?: string;                 // thinking/reasoning trace, when exposed
  toolCalls: ToolCallRequest[];
  usage: TokenUsage;
  finishReason?: string;              // "stop", "length", "tool_calls", ...
}

interface ToolCallRequest { id: string; name: string; args: unknown; }
interface ResponseFormatWire { name: string; schema: Record<string, unknown>; strict?: boolean; }
```

`finishReason` lets consumers detect truncation; a TOTAL truncation (no content
at all) throws `MODEL_ERROR` at the provider instead.

### `MockModelProvider`

```ts
class MockModelProvider implements ModelProvider {
  readonly id = "mock";
  async complete(_options: CompleteOptions): Promise<ModelCompletion>;
}
```

Deterministic mock for tests/examples — no network.

### Provider prefixes (`initChatModel`)

```ts
function initChatModel(
  spec: string,
  opts?: {
    apiKey?: string;
    baseUrl?: string;
    temperature?: number;
    maxTokens?: number;
    stream?: boolean;
    thinkingBudget?: number;
  },
): ModelProvider;
```

Resolves `provider:model` strings to a configured provider:

| Prefix | Provider | Key | Notes |
|---|---|---|---|
| `together:<id>` or bare `<id>` | TogetherAI (OpenAI-compatible) | `TOGETHER_API_KEY` | the default |
| `openai:<id>` | OpenAI | `OPENAI_API_KEY` | |
| `anthropic:<id>` | Anthropic native Messages API | `ANTHROPIC_API_KEY` | `thinkingBudget` supported |
| `ollama:<id>` | local Ollama | none | `OLLAMA_BASE_URL` or `localhost:11434` |
| `custom:<id>` | any OpenAI-compatible endpoint | `apiKey` opt | needs `baseUrl` + `apiKey` |

Unknown prefixes throw with the valid list.

### `OpenAICompatibleProvider`

```ts
interface OpenAICompatibleOptions {
  baseUrl: string;
  apiKey: string;
  model: string;
  temperature?: number;   // default 0.2
  maxTokens?: number;
  stream?: boolean;       // SSE so text + thinking flow via callbacks
  fetchFn?: typeof fetch; // injectable for tests
}

class OpenAICompatibleProvider implements ModelProvider {
  constructor(opts: OpenAICompatibleOptions);
  readonly id: string; // "openai-compatible:<model>"
  capabilities = { structuredOutput: true, streaming: true };
  async complete(options: CompleteOptions): Promise<ModelCompletion>;
}
```

Minimal OpenAI-compatible chat provider (OpenAI, TogetherAI, vLLM, ...):
`POST {baseUrl}/chat/completions` with tools, no SDK dependency. Images fail
fast with `MODEL_CAPABILITY` when the model can't see; thinking streams via
`onThinking` (Together's `reasoning` and vLLM/DeepSeek's `reasoning_content`
spellings both work). Streaming requests `stream_options: {include_usage: true}`
so usage lands on the final chunk. Guards: a `stop` with the entire answer in
the reasoning channel (empty content) throws retryable `MODEL_ERROR`; a
`length` stop with zero usable content throws a non-retryable `MODEL_ERROR`.

### `providerFromEnv`

```ts
function providerFromEnv(
  active: { baseUrl: string; apiKey: string; model: string },
  extra?: Partial<OpenAICompatibleOptions>,
): OpenAICompatibleProvider;
```

Build a provider from env config (TogetherAI preferred when its key is present).

### `AnthropicProvider`

```ts
interface AnthropicOptions {
  apiKey: string;
  model: string;
  baseUrl?: string;           // default https://api.anthropic.com
  maxTokens?: number;         // default 1024
  temperature?: number;
  thinkingBudget?: number;   // extended thinking budget (tokens)
  stream?: boolean;
  fetchFn?: typeof fetch;
}

class AnthropicProvider implements ModelProvider {
  constructor(opts: AnthropicOptions);
  readonly id: string; // "anthropic:<model>"
  capabilities = { structuredOutput: false, streaming: true };
  async complete(options: CompleteOptions): Promise<ModelCompletion>;
}
```

Native Anthropic Messages API (fetch-based, no SDK). Tools become `tools` with
`input_schema`; tool results become `tool_result` blocks grouped into a single
user message per assistant turn; thinking blocks stream via `onThinking`. There
is no native `json_schema` mode — `capabilities.structuredOutput` is false, so
`auto` responseFormat correctly falls back to the tool strategy. The constructor
fails loudly when `maxTokens <= thinkingBudget`. Stop reason `max_tokens` is
normalized to `length`.

### `OllamaProvider` / `ollama`

```ts
interface OllamaOptions {
  model: string;
  baseUrl?: string;      // default http://localhost:11434/v1
  temperature?: number;
  maxTokens?: number;
  stream?: boolean;
  fetchFn?: typeof fetch;
}

class OllamaProvider extends OpenAICompatibleProvider {
  constructor(opts: OllamaOptions);
  override readonly id: string; // "ollama:<model>"
}

function ollama(opts: OllamaOptions): OllamaProvider;
```

Local-first Ollama via its OpenAI-compatible endpoint: no API key, no account,
no network beyond localhost. `structuredOutput` stays false (`auto` uses the
tool strategy).

### Capabilities (`capabilities.ts`)

```ts
type ModelCapability = "vision" | "tools" | "reasoning-stream";

function supportsVision(modelId: string): boolean;
function assertCanHandle(modelId: string, needsVision: boolean): void;
```

Model capability registry — explicit allowlist + fail-fast
(`AgentError(MODEL_CAPABILITY)`) when input needs vision the model lacks.

### Pricing (`pricing.ts`)

```ts
interface ModelPrice { inputPerMillion: number; outputPerMillion: number; }

function priceFor(modelId: string): ModelPrice | undefined;
function usageCost(usage: TokenUsage, modelId: string, override?: ModelPrice): number | undefined;
```

Per-model USD prices per 1M tokens, computed client-side because
OpenAI-compatible and Anthropic usage payloads carry token counts only.
`ollama:` ids are priced free (a local name colliding with a cloud model runs
free and must win over the priced table). Unknown models return `undefined`
from `usageCost` — never a misleading zero. Reasoning tokens are already inside
`outputTokens`, so they are billed at the output rate with no extra addition.

---

## Memory (`memory/`, `memory/compaction.ts`)

### `Memory` / `InMemoryMemory` / `FileMemory`

```ts
interface Memory {
  load(): Promise<ChatMessage[]>;
  save(messages: ChatMessage[]): Promise<void>; // APPENDS (windows to max)
  clear(): Promise<void>;
}

class InMemoryMemory implements Memory {
  constructor(maxMessages = 50);
}

class FileMemory implements Memory {
  /** Persists per write to <dir>/memory.json (write-then-rename atomicity). */
  constructor(dir: string, maxMessages = 50);
}
```

A corrupted memory file fails loudly with `AgentError(MEMORY_CORRUPT)` instead
of silently wiping the disk on the next save; a missing file means fresh memory.
The tmp file per flush is unique, so concurrent saves can't race on the rename.

### Compaction (`memory/compaction.ts`)

```ts
interface CompactionOptions {
  /** Compact when messages exceed this count. Default 40. */
  maxMessages?: number;
  /** Summarizer: (messages to compress) -> summary text. Default: extractive stub. */
  summarize?: (messages: ChatMessage[]) => Promise<string>;
}

function defaultSummarize(messages: ChatMessage[]): Promise<string>;

class CompactedMemory implements Memory {
  constructor(inner: Memory, opts?: CompactionOptions);
}
```

Wrap any Memory with auto-compaction: overflow messages are replaced by a
single summary boundary message so the stored history shrinks to exactly the
cap.

---

## Conversation managers (`conversation/`)

```ts
interface ConversationManager {
  readonly name: string;
  /** Messages removed so far (lets session loading skip efficiently). */
  removedCount: number;
  /** After-run management (before memory.save). */
  apply(messages: ChatMessage[]): ChatMessage[] | Promise<ChatMessage[]>;
  /** Explicit shrink (overflow recovery / proactive triggers). */
  reduce(messages: ChatMessage[]): ChatMessage[] | Promise<ChatMessage[]>;
  /** Per-turn management inside the loop (sliding window). */
  beforeModel?(messages: ChatMessage[], step: number): ChatMessage[] | Promise<ChatMessage[]>;
}
```

Pinned messages are never removed; tool/assistant-tool_call pairs are never
orphaned.

### `trimDangling` / `messageTokens` / `historyTokens`

```ts
function trimDangling(messages: ChatMessage[]): ChatMessage[];
function messageTokens(m: ChatMessage): number;
function historyTokens(messages: ChatMessage[]): number;
```

`trimDangling` drops leading messages that lost their pair (tool head,
call-only assistant head); pinned messages stop the scan.

### `NullConversationManager`

```ts
class NullConversationManager implements ConversationManager { }
```

Keeps full history — pass explicitly to disable trimming.

### `SlidingWindowConversationManager`

```ts
interface SlidingWindowOptions {
  windowSize?: number;  // max unpinned messages kept. Default 40.
  pinFirst?: number;    // permanently protect the first N messages. Default 0.
  perTurn?: boolean | number; // before every model call (true) or every N calls
}

class SlidingWindowConversationManager implements ConversationManager {
  constructor(opts?: SlidingWindowOptions);
}
```

Kept messages merge in ORIGINAL order (pinned messages are never hoisted to
the front).

### `SummarizingConversationManager`

```ts
interface SummarizingOptions {
  summaryRatio?: number;   // fraction of oldest messages to summarize (0.1–0.8). Default 0.3.
  preserveRecent?: number; // newest messages always kept. Default 10.
  pinFirst?: number;
  summarizer?: (messages: ChatMessage[]) => Promise<string>;
  model?: ModelProvider;   // used for summaries when no summarizer fn is given
  summaryPrefix?: string;  // default "Summary of earlier conversation:"
}

class SummarizingConversationManager implements ConversationManager {
  constructor(opts?: SummarizingOptions);
  /** Set when the last apply() summarized (tests/UI can assert on it). */
  lastSummary?: string;
}
```

Without a summarizer or model, the head is joined as raw transcript lines
(extractive fallback).

---

## Sessions (`sessions/`)

### `Storage` / `InMemoryStorage` / `LocalFileStorage`

```ts
interface Storage {
  read(key: string): Promise<string | undefined>;
  write(key: string, value: string): Promise<void>;
  deletePrefix(prefix: string): Promise<void>;
  list(prefix: string): Promise<string[]>;
}

class InMemoryStorage implements Storage { }
class LocalFileStorage implements Storage {
  constructor(root: string);
}
```

Byte-moving backends. `LocalFileStorage` rejects traversal keys with
`AgentError(STORAGE_KEY_INVALID)` (`..` segments or NULs).

### `Snapshot` / `TakeSnapshotOptions` / `selectSnapshotFields`

```ts
interface SnapshotData {
  messages?: ChatMessage[];
  state?: Record<string, unknown>;
  systemPrompt?: string;
}

interface Snapshot {
  id: string;
  createdAt: string;
  data: SnapshotData;
  appData?: Record<string, unknown>;
}

type SnapshotField = "messages" | "state" | "systemPrompt";

interface TakeSnapshotOptions {
  preset?: "session";               // messages + state
  include?: SnapshotField[];
  exclude?: SnapshotField[];
  appData?: Record<string, unknown>;
}

function selectSnapshotFields(
  full: Required<Pick<SnapshotData, "messages" | "state">> & Pick<SnapshotData, "systemPrompt">,
  opts?: TakeSnapshotOptions,
): SnapshotData;
```

### `SessionManager`

```ts
interface SessionManagerOptions {
  sessionId?: string; // generated UUID when omitted
  storage: Storage;
  agentId?: string;   // default "default"
}

class SessionManager {
  readonly sessionId: string;
  readonly agentId: string;
  constructor(opts: SessionManagerOptions);
  async saveLatest(data: SnapshotData): Promise<void>;
  async loadLatest(): Promise<SnapshotData | undefined>;
  /** Immutable checkpoint with a UUID id; returns the id. */
  async saveSnapshot(data: SnapshotData, appData?: Record<string, unknown>): Promise<string>;
  async listSnapshotIds(): Promise<string[]>;
  async restoreSnapshot(id: string): Promise<Snapshot | undefined>;
  async deleteSession(): Promise<void>;
}
```

Latest snapshot + immutable checkpoints, scoped
`session/{id}/scopes/agent/{agentId}/snapshots`.

---

## Checkpoint (`checkpoint/`)

```ts
interface ThreadCheckpoint {
  threadId: string;
  savedAt: string;
  messages: ChatMessage[];
  state: Record<string, unknown>;
  pending: { interruptId: string; tool: string; args: unknown }[];
}

interface Checkpointer {
  save(threadId: string, checkpoint: Omit<ThreadCheckpoint, "threadId" | "savedAt">): Promise<void>;
  load(threadId: string): Promise<ThreadCheckpoint | undefined>;
  delete(threadId: string): Promise<void>;
}

async function withFileLock<T>(
  lockPath: string,
  fn: () => Promise<T>,
  opts?: { staleMs?: number }, // default 30000
): Promise<T>;

class FileCheckpointer implements Checkpointer {
  /** <root>/threads/<threadId>/checkpoint.json, lock-guarded. */
  constructor(root: string);
}
```

Local-first durability: file checkpointer + file locks. No services, no
third-party deps — checkpointed HITL resume works fully offline.
`withFileLock` is exclusive via `O_EXCL` creation; stale locks are reclaimed.
`threadId` lands in a filesystem path, so it must be a single safe directory
name (`THREAD_ID_INVALID` otherwise). Single-machine correctness; distributed
locking is explicitly out of scope.

---

## Middleware (`middleware/`)

Execution order (pinned): `beforeModel` / `beforeTool` in registration order;
`afterModel` / `afterTool` in REVERSE order; `wrapModel` / `aroundTool` nested,
first registered = outermost.

### `AgentMiddleware` and hook shapes

```ts
interface HookContext {
  step: number;
  /** Per-run data, shared by reference (never sent to the model). */
  invocationState?: InvocationState;
}

interface ModelRequest {
  messages: ChatMessage[];
  tools: { name: string; description: string; parametersSchema?: Record<string, unknown> }[];
  responseFormat?: ResponseFormatWire;
}

interface ToolCallRef { name: string; args: unknown; }

interface AgentMiddleware {
  name: string;
  /** Tools contributed by this middleware (merged into the agent's tool list). */
  tools?: Tool[];
  beforeModel?: (req: ModelRequest, ctx: HookContext) => Promise<ModelRequest | undefined> | ModelRequest | undefined;
  wrapModel?: (req: ModelRequest, next: (req: ModelRequest) => Promise<ModelCompletion>, ctx: HookContext) => Promise<ModelCompletion>;
  afterModel?: (res: ModelCompletion, req: ModelRequest, ctx: HookContext) => Promise<ModelCompletion | undefined> | ModelCompletion | undefined;
  beforeTool?: (call: ToolCallRef, ctx: HookContext) => Promise<ToolCallRef | undefined> | ToolCallRef | undefined;
  aroundTool?: (call: ToolCallRef, next: () => Promise<unknown>, ctx: HookContext) => Promise<unknown>;
  afterTool?: (call: ToolCallRef, output: unknown, ctx: HookContext) => Promise<unknown> | unknown;
}

function createMiddleware(m: AgentMiddleware): AgentMiddleware;
```

`createMiddleware` is an identity factory — documents intent and locks the type.

### `ToolRejection`

```ts
class ToolRejection extends Error {
  readonly feedback: string;
  constructor(feedback: string);
}
```

Thrown (usually by approval middleware) to skip execution with feedback for the
model — the loop records it as the tool output and continues.

### Retry verdicts: `stampRetryable` / `getRetryable`

```ts
function stampRetryable<T extends Error>(err: T, retryable: boolean): T;
function getRetryable(err: unknown): boolean | undefined;
```

Mark an error retryable/fatal without touching its shape or type (the verdict
rides a well-known global symbol); read it back with `getRetryable` — an
`AgentError` maps to its own `retryable` flag. Full contract on
[Errors & retry contract](errors.md).

### `BackoffOptions` / `RetryMiddlewareOptions`

```ts
interface BackoffOptions {
  maxRetries?: number;      // default 2
  initialDelayMs?: number;  // default 1000
  backoffFactor?: number;   // default 2
  maxDelayMs?: number;      // default 60000
  jitter?: boolean;         // default true
}

interface RetryMiddlewareOptions extends BackoffOptions {
  retryOn?: ((error: Error) => boolean) | (new (...args: never[]) => Error)[];
  onFailure?: "error" | "continue" | ((error: Error) => string);
  /** Limit retries to these tool names (tool retry only). */
  tools?: string[];
}
```

### Retry middlewares

```ts
function toolRetry(opts: { attempts?: number; backoffMs?: number } = {}): AgentMiddleware;
function toolRetryMiddleware(opts?: RetryMiddlewareOptions): AgentMiddleware;
function modelRetryMiddleware(opts?: RetryMiddlewareOptions): AgentMiddleware;
```

- `toolRetry` — simple fixed attempts/backoff; `AgentError`s pass through untouched.
- `toolRetryMiddleware` / `modelRetryMiddleware` — exponential backoff + jitter,
  error-class filtering, per-tool scoping, and exhausted-failure behavior
  (`onFailure: "continue"` degrades the error to feedback text — never for
  non-retryable verdicts). Model retry's `"continue"` synthesizes an
  empty-text completion so the run ends gracefully. Both honor `stampRetryable`
  verdicts: `getRetryable(err) === false` is never retried (even with
  `onFailure: "continue"`), and non-`Error` throwables always retry.
- `backoffDelayMs(attempt, opts)` — computed delay with jitter, exported for reuse.

### `modelFallbackMiddleware`

```ts
function modelFallbackMiddleware(...fallbacks: ModelProvider[]): AgentMiddleware;
```

Fall back across providers when the primary model fails (first success wins;
the last error is re-raised when all fail).

### Budget middlewares

```ts
function modelCallLimit(maxCalls: number): AgentMiddleware;

function toolCallLimitMiddleware(opts: {
  toolName?: string;
  runLimit: number;                  // required
  exitBehavior?: "continue" | "error";
}): AgentMiddleware;
```

`modelCallLimit`: exceeded -> `AgentError(BUDGET_EXCEEDED)`; stateless (uses
`ctx.step`). `toolCallLimitMiddleware`: per-run tool-call budgets (global or
per-tool); exceeded calls are blocked with `ToolRejection` feedback
(`continue`) or raise `BUDGET_EXCEEDED` (`error`).

### Human-in-the-loop

```ts
type HitlDecision = "approve" | { edit: unknown } | { reject: string } | { suspend: true };

class HitlInterrupt extends Error {
  readonly interruptId: string;
  readonly tool: string;
  readonly args: unknown;
}

function hitlKey(tool: string, args: unknown): string;

interface HitlDecideRequest {
  name: string;
  args: unknown;
  allowed: ("approve" | "edit" | "reject")[];
  interruptId: string;
}

function humanInTheLoopMiddleware(opts: {
  interruptOn: Record<string, boolean | { allowedDecisions?: ("approve" | "edit" | "reject")[] }>;
  /** Inline decider. May return { suspend: true }; omit to always suspend. */
  decide?: (req: HitlDecideRequest) => Promise<HitlDecision> | HitlDecision;
}): AgentMiddleware;
```

Pause tool execution for approval/edit/reject. Decisions are read from
`invocationState.__hitlDecisions` by exact `hitlKey(tool, args)` first, then by
bare tool name (the model re-issues calls with regenerated arg text, so exact
keys alone never converge). Disallowed decisions raise
`AgentError(HITL_DECISION)`. `HitlInterrupt` is persisted by the checkpointer
when `checkpointer` + `threadId` are set on the run.

### PII

```ts
interface PIIMatch { text: string; start: number; end: number; }
type PIIDetector = RegExp | string | ((content: string) => PIIMatch[]);
type PIIStrategy = "block" | "redact" | "mask" | "hash";

function piiMiddleware(
  piiType: string,
  opts?: {
    strategy?: PIIStrategy;            // default "redact"
    detector?: PIIDetector;
    applyToInput?: boolean;             // default true
    applyToOutput?: boolean;
    applyToToolResults?: boolean;
  },
): AgentMiddleware;
```

Built-in detectors: `email`, `credit_card`, `ip`. Strategies: `[REDACTED_TYPE]`,
mask (last 4 visible), djb2 hash, or `AgentError(PII_BLOCKED)`.

### Summarization + context editing

```ts
function summarizationMiddleware(opts: {
  model: ModelProvider;
  triggerTokens: number;
  keepMessages?: number;        // default 20
  summaryPrefix?: string;
  summarize?: (messages: ChatMessage[]) => Promise<string>;
}): AgentMiddleware;

interface ClearToolUsesOptions {
  triggerTokens?: number;   // default 100000
  keep?: number;            // newest results kept, default 3
  clearToolInputs?: boolean; // default false
  excludeTools?: string[];
  placeholder?: string;     // default "[cleared]"
}

class ClearToolUsesEdit {
  constructor(opts?: ClearToolUsesOptions);
  apply(messages: ChatMessage[]): ChatMessage[];
  clearInputs(messages: ChatMessage[]): ChatMessage[];
}

function contextEditingMiddleware(opts: { edits?: ClearToolUsesEdit[] }): AgentMiddleware;
```

`summarizationMiddleware` auto-summarizes history when estimated tokens pass
`triggerTokens` (text-oriented: summarized-away multimodal survives only as
prose). `contextEditingMiddleware` clears older tool outputs past a token
budget (or strips tool-call/result pairs — see the Python deviation notes on
the [parity page](../reference/parity.md)).

### Todo list

```ts
interface TodoItem { content: string; status: "pending" | "active" | "completed"; }

function todoListMiddleware(): AgentMiddleware & { listTodos(): TodoItem[] };
```

Contributes a `write_todos` tool, tracks state per run, and hints the system
prompt on step 0.

### LLM tool selector / tool emulator

```ts
function llmToolSelectorMiddleware(opts: {
  model: ModelProvider;
  maxTools?: number;
  alwaysInclude?: string[];
  systemPrompt?: string;
}): AgentMiddleware;

function toolEmulatorMiddleware(opts: { tools?: string[]; model?: ModelProvider } = {}): AgentMiddleware;
```

`llmToolSelectorMiddleware` narrows 10+ tools to the relevant few before the
main model call; `toolEmulatorMiddleware` emulates tool execution with an LLM —
testing/prototyping without side effects.

### Filesystem tools / subagents

```ts
function filesystemTools(root: string): Tool[];
function filesystemMiddleware(opts: { root: string }): AgentMiddleware;

function createSubAgentMiddleware(opts: {
  model: ModelProvider;
  subagents: { name: string; description: string; systemPrompt?: string; tools?: Tool[] }[];
}): AgentMiddleware;
```

- `filesystemTools` — `ls`/`read_file`/`write_file`/`edit_file` scoped to
  `root`; escapes raise `FS_ESCAPE`, edit misses `FS_NO_MATCH`.
- `createSubAgentMiddleware` — a `task` tool that delegates to named
  specialists (isolates context); unknown names raise `SUBAGENT_UNKNOWN`.

---

## Runtime (`runtime/`)

```ts
interface Budget {
  signal?: AbortSignal;
  timeoutMs?: number;
}

async function runWithBudget<T>(fn: () => Promise<T>, budget?: Budget): Promise<T>;
```

Runtime guard: abort + timeout. Abort converts to `AgentError(ABORTED)`
(non-retryable); the timeout raises `AgentError(TIMEOUT, ..., retryable=true)`.
Token/cost budgets live in the agent loop.

---

## Observability (`observability/`)

```ts
interface StepEvent { step: number; messages: ChatMessage[]; }
interface ToolCallEvent { tool: string; args: unknown; }
interface ToolResultEvent { tool: string; output: unknown; }
interface DoneEvent { text: string; toolCalls: AgentResult["toolCalls"]; }

/** Observability hooks — plug logger/tracer here, keep core dependency-free. */
interface AgentEvents {
  onStep?: (e: StepEvent) => void;
  onToolCall?: (e: ToolCallEvent) => void;
  onToolResult?: (e: ToolResultEvent) => void;
  onDone?: (e: DoneEvent) => void;
  onToken?: (delta: string) => void;    // streamed text deltas
  onThinking?: (delta: string) => void; // streamed thinking deltas
}

function supportsColor(): boolean;
function consoleEvents(prefixOrOpts?: string | ConsoleEventsOptions, maybeOpts?): AgentEvents;
function logError(prefix: string, err: unknown, colors?: boolean): void;

interface ConsoleEventsOptions {
  prefix?: string;
  colors?: boolean; // force colors on/off; default: TTY without NO_COLOR
}
```

`consoleEvents` prints per-step/tool/done lines and streams tokens to stdout;
`logError` prints a red error line and pairs with it.

---

## Tracing (`tracing/`)

```ts
interface Span {
  traceId: string;
  spanId: string;
  parentId?: string;
  name: string;
  startedAt: string;
  endedAt?: string;
  attributes: Record<string, unknown>;
}

interface SpanSink { emit(span: Span): void; }

class ConsoleSpanSink implements SpanSink { }
class JsonlSpanSink implements SpanSink { constructor(path: string) {} }
class MemorySpanSink implements SpanSink { spans: Span[] }

function tracingEvents(sink: SpanSink, opts?: { traceId?: string }): AgentEvents;
```

OTel-shaped spans from `AgentEvents`, sunk to console or JSONL — no OTel SDK;
the shape is compatible, the transport is local-first. Span names:
`agent.step`, `tool.call` -> `tool.result` (parented), `model.token`,
`model.thinking`, `agent.done`.

### `policyFilterMiddleware` (also lives here)

```ts
function policyFilterMiddleware(opts: {
  allow?: (string | RegExp)[];
  deny?: (string | RegExp)[];
  exitBehavior?: "continue" | "error";
}): AgentMiddleware;
```

Allow/deny tool calls by name pattern before execution. Denied calls become
`ToolRejection` feedback (run continues) or raise `AgentError(POLICY_DENY)` with
`exitBehavior: "error"`. `g`-flagged regexes are re-cloned statelessly so one
tool name can't alternate denied/allowed.

---

## Orchestration (`orchestration/`)

```ts
type OrchestrationStrategy = "sequential" | "parallel" | "router";

interface OrchestrationPlan {
  strategy: OrchestrationStrategy;
  agents: Agent[];
  /** Max concurrent agents for parallel. Default: all at once. */
  concurrency?: number;
  /** Model that picks the agent for router. Defaults to the first agent's model. */
  routerModel?: ModelProvider;
  events?: AgentEvents;
}

interface OrchestratedResult {
  strategy: OrchestrationStrategy;
  results: AgentResult[];
  /** Combined text: last result (sequential/router), joined (parallel). */
  text: string;
  /** Router only: index of the chosen agent. */
  routedTo?: number;
}

function runOrchestrated(
  plan: OrchestrationPlan,
  input: string,
  signalOrOpts?: AbortSignal | RunOptions,
): Promise<OrchestratedResult>;
```

- `sequential` — chain: each agent receives the previous agent's text appended
  to the input.
- `parallel` — fan-out with a concurrency cap; results keep agent order.
- `router` — one model call picks the agent by index; only that agent runs
  (`AgentError(ROUTER_NO_MATCH)` on an invalid pick).
- An empty agent list raises `AgentError(ORCHESTRATION_EMPTY)`.

---

## Workflows (`workflows/`)

```ts
interface WorkflowNode {
  id: string;
  /** ctx maps completed node ids to their results. */
  run: (ctx: Record<string, unknown>, invocationState?: InvocationState) => Promise<unknown>;
  dependsOn?: string[];
  /** Retry policy. Default: no retry. Abort always rethrows. */
  retry?: { attempts?: number; backoffMs?: number };
}

interface WorkflowOptions {
  signal?: AbortSignal;
  invocationState?: InvocationState;
}

async function runWorkflow(nodes: WorkflowNode[], opts?: WorkflowOptions):
  Promise<Record<string, unknown>>;
```

Execute a DAG: topological levels run in order, nodes within a level run
concurrently; returns id -> result. Raised errors are structured `WORKFLOW_*`
(`WORKFLOW_DUPLICATE_ID`, `WORKFLOW_UNKNOWN_DEP`, `WORKFLOW_CYCLE`).
Duplicate `dependsOn` entries count as one edge.

---

## Swarm (`swarm/`)

```ts
interface SwarmOptions {
  agents: Agent[];
  /** First agent to run. Default: agents[0]. Accepts instance or name. */
  entryPoint?: Agent | string;
  maxHandoffs?: number;          // default 20
  maxIterations?: number;        // default 20
  executionTimeoutMs?: number;
  nodeTimeoutMs?: number;
  /** Ping-pong detection: window size + min unique agents (0 = disabled). */
  loopDetectionWindow?: number;
  minUniqueAgents?: number;
}

interface HandoffSignal {
  to: string;
  message: string;
  context?: Record<string, unknown>;
}

type SwarmStatus = "COMPLETED" | "FAILED";

interface SwarmResult {
  status: SwarmStatus;
  text: string;
  nodeHistory: string[];
  results: Record<string, AgentResult>;
  iterations: number;
  handoffs: number;
  usage: TokenUsage;
  sharedContext: Record<string, Record<string, unknown>>;
  error?: string;
}

type SwarmStreamEvent =
  | { type: "node_start"; agent: string }
  | { type: "node_done"; agent: string }
  | { type: "handoff"; from: string; to: string }
  | { type: "agent_event"; agent: string; event: StreamEvent }
  | { type: "result"; result: SwarmResult };

function readHandoffSignal(err: unknown): HandoffSignal | undefined;

class Swarm {
  constructor(opts: SwarmOptions);
  run(task: string | ContentBlock[], opts?: RunOptions & { sharedState?: InvocationState }): Promise<SwarmResult>;
  stream(task: string | ContentBlock[], opts?: RunOptions & { sharedState?: InvocationState }):
    AsyncGenerator<SwarmStreamEvent, SwarmResult>;
}
```

The `handoff_to_agent` tool contract: each member gets it via a `withTools()`
clone (model/tools/middleware shared; memory/sessions/state fresh per turn —
shared knowledge flows through the swarm input). Calling it with an unknown
agent name returns model-visible feedback; a valid call throws
`AgentError(SWARM_HANDOFF)` carrying the `HandoffSignal`, and the swarm routes
to the next member. Loop detection: `loopDetectionWindow=0` (default) disables
it; setting a window alone enables detection with `minUniqueAgents`
defaulted to 2 — a window of fewer entries than the window size is ignored, and
fewer unique agents than the threshold in the last `window` trail entries fails
the swarm with a `Repetitive handoff detected ...` error.

Construction raises `SWARM_EMPTY` (no agents) and `SWARM_UNKNOWN_ENTRY`.

---

## A2A and serving

### A2A client (`a2a/`)

```ts
interface AgentCard {
  name: string;
  description?: string;
  version?: string;
  skills?: { name: string; description?: string }[];
}

interface A2AClientOptions { fetchFn?: typeof fetch; timeoutMs?: number; }

class A2AClient {
  constructor(endpoint: string, opts?: A2AClientOptions); // default timeout 300s
  async getCard(): Promise<AgentCard>;
  async send(text: string, contextId?: string): Promise<{ text: string; contextId?: string }>;
}

function a2aAsTool(client: A2AClient, opts?: { name?: string; description?: string }): Tool;
```

Consume a remote A2A agent like a local one: card discovery via
`/.well-known/agent-card.json`, JSON-RPC `message/send` over HTTP. The context
id is read from the RESULT (servers mint ids) — call `a2aAsTool` to wrap a
remote agent as a Tool for orchestrators (`remote_agent` by default,
`{ input, contextId? }` args).

### A2A server

```ts
interface A2AServerOptions {
  card: AgentCard;
  /** Fresh agent per contextId (isolation), retained up to maxContexts (LRU). */
  agentFactory: (contextId: string) => Agent | Promise<Agent>;
  maxContexts?: number;   // default 1000
  port?: number;
  host?: string;
  /** Durable HITL threads: runs execute with { checkpointer, threadId: contextId }. */
  checkpointer?: Checkpointer;
  /** Browser frontends: reflected as Access-Control-Allow-Origin. */
  corsOrigins?: string[];
  /** Max JSON-RPC request body bytes. Default 10 MiB. */
  maxBodyBytes?: number;
}

class A2AServer {
  constructor(opts: A2AServerOptions);
  async start(port?: number, host?: string): Promise<number>; // returns the actual port
  async stop(): Promise<void>;
}
```

Serve a Framework agent over A2A: `GET /ok` + `/health`, the card endpoint,
`POST /` (`message/send`), and `POST /stream` (SSE of stream events; a client
disconnect aborts the agent run; the final frames are
`{contextId, done, text}` then `[DONE]`). Request bodies beyond `maxBodyBytes`
fail with `A2A_BODY_TOO_LARGE` (HTTP 413). Never share one agent instance
across contexts — shared history leaks conversations between users.

### Frontend client (`client/`) { #client-module }

```ts
interface AgentClientOptions { fetchFn?: typeof fetch; timeoutMs?: number; }

class AgentClient {
  constructor(endpoint: string, opts?: AgentClientOptions);
  async card(): Promise<AgentCard>;
  async send(text: string, contextId?: string): Promise<{ text: string; contextId?: string }>;
  /** Live StreamEvents over SSE (POST /stream). Throws AgentError on server errors. */
  async *stream(
    text: string,
    contextId?: string,
    signal?: AbortSignal,
    opts?: { onContext?: (contextId: string) => void },
  ): AsyncGenerator<StreamEvent>;
}

async function handleAgentRoute(req: Request, client: AgentClient): Promise<Response>;
```

Framework-agnostic client (fetch + async iterators) for frontends and server
components. Early break/throw/abort releases the connection and stops the
upstream agent run instead of letting it finish in the background.
`handleAgentRoute` is the Next.js / TanStack Start route-handler bridge:
`POST {text, contextId?}` -> SSE stream of agent events. Mount it at
`app/api/agent/stream/route.ts` and point `useAgent({ endpoint: "/api/agent" })`
at the parent; the browser disconnect (`req.signal`) stops the upstream run.

### framework.json serving (`serve/`)

```ts
type FrameworkFile = z.infer<typeof FrameworkFileSchema>;
type ResolvedAgentDef = z.infer<typeof AgentDefSchema>;
type ServedAgent = { name: string; url: string; cardUrl: string; server: A2AServer };

interface LoadedFrameworkFile {
  server: { host: string; basePort: number };
  envFile?: string | string[];
  env?: Record<string, string>;
  checkpointer?: { dir: string };
  http: { corsOrigins: string[] };
  agents: ResolvedAgentDef[];
}

function loadEnvFile(path: string, override = true): void;
function loadFrameworkConfig(path: string): LoadedFrameworkFile;
async function buildServedAgent(
  def: ResolvedAgentDef,
  opts?: { sessionId?: string },
): Promise<{ agent: Agent; card: { name: string; description?: string } }>;
async function serveFramework(file: LoadedFrameworkFile):
  Promise<{ agents: ServedAgent[]; stop: () => Promise<void> }>;
```

Declarative serving: relative paths resolve against the config file's
directory; agent entries are inline or `{ "file": "./agents/helper.json" }`
references (+ optional inline overrides). An agent def supports `name`,
`description`, `systemPrompt`, `model`, `port`, `skills` (paths; the catalog is
appended to the system prompt), `filesystem` (root for filesystem middleware),
`todos`, `maxSteps` (default 8), `sessionDir`, and `envFile` (keys override
framework-level env). `serveFramework` boots one A2AServer per agent (fresh
agent per contextId; ports assign sequentially from `basePort`, default 8484).
The `framework-serve` CLI in `dist/cli.js` wraps the same flow.

---

## MCP (`mcp/`)

```ts
interface McpToolDef {
  name: string;
  description?: string;
  inputSchema: Record<string, unknown>;
}

interface McpClientOptions {
  fetchFn?: typeof fetch;
  headers?: Record<string, string>;
  clientName?: string;
}

function jsonSchemaToZod(schema: Record<string, unknown>): z.ZodType<unknown>;

class McpHttpClient {                    // Streamable HTTP transport
  readonly serverId: string;
  constructor(url: string, opts?: McpClientOptions & { serverId?: string });
  async connect(): Promise<{ name: string; version?: string }>;
  async listTools(): Promise<McpToolDef[]>;
  async callTool(name: string, args: unknown): Promise<unknown>;
  async close(): Promise<void>;
}

class McpStdioClient {                   // spawned server, line-delimited JSON-RPC
  readonly serverId: string;
  constructor(command: string, args?: string[], opts?: { serverId?: string; clientName?: string });
  async connect(): Promise<void>;         // child crash/exit settles in-flight requests
  async listTools(): Promise<McpToolDef[]>;
  async callTool(name: string, args: unknown): Promise<unknown>;
  async close(): Promise<void>;
}

type AnyMcpClient = Pick<McpHttpClient, "serverId" | "listTools" | "callTool" | "close">;

interface McpClient {
  readonly serverId: string;
  listTools(): Promise<McpToolDef[]>;
  callTool(name: string, args: unknown): Promise<unknown>;
  close(): Promise<void>;
}

async function mcpToolsToFramework(client: McpClient, defs?: McpToolDef[]): Promise<Tool[]>;
async function connectMcpHttp(url: string, opts?: McpClientOptions): Promise<McpHttpClient>;
async function connectMcpStdio(command: string, args?: string[]): Promise<McpStdioClient>;
```

`jsonSchemaToZod` converts JSON Schema input schemas to zod (objects, enums,
primitives, arrays; `unknown` otherwise). `mcpToolsToFramework` bridges MCP
tool defs to framework Tools, namespacing as `server.tool` on name collision. An
SSE response carrying only notifications throws `MCP_PROTOCOL`; JSON-RPC errors
raise `MCP_ERROR`; transport failures `MCP_TRANSPORT`; tool failures
`MCP_TOOL_ERROR`; using a client before `connect()` raises `MCP_NOT_CONNECTED`.

---

## Skills and prompts

### Skills (`skills/`)

```ts
interface Skill {
  name: string;
  description: string;
  version: string;
  path: string;
  body: string;
  tools?: Tool[];
}

interface SkillLoaderOptions {
  dirs: string[];
  strict?: boolean; // throw on invalid skill; else skip. Default false.
}

function parseSkillFile(content: string, path?: string): Omit<Skill, "tools">;

class SkillRegistry {
  register(skill: Skill): this;
  get(name: string): Skill | undefined;
  list(): Skill[];
  /** Expose each skill's tools (if any) for Agent consumption. */
  asTools(): Tool[];
}

async function discoverSkills(dirs: string[], opts?: { strict?: boolean }): Promise<Skill[]>;
async function loadSkillsInto(registry: SkillRegistry, dirs: string[], opts?: { strict?: boolean }): Promise<Skill[]>;

function tokenize(text: string): string[];
function scoreSkills(registry: SkillRegistry, input: string): { skill: Skill; score: number }[];
function matchSkills(registry: SkillRegistry, input: string, threshold = 0.05): Skill[];
```

A skill is a directory with `SKILL.md` frontmatter (name, description, version)
+ markdown body. Discovery scans one level plus direct files; invalid skills
are skipped unless `strict`. Routing is dependency-free TF-IDF cosine over
skill name+description; explicit `$name` mentions always win (word-boundary
matched). Activation is explicit — never magic.

### Prompts (`prompts/`)

```ts
interface PromptTemplate {
  name: string;
  version: string;
  template: string;
  render(vars: Record<string, string | number>): string;
}

function definePrompt(name: string, template: string, version = "0.1.0"): PromptTemplate;
function loadPromptFile(path: string): PromptTemplate;
function abPrompts(
  variants: { name: string; template: string; version?: string; weight?: number }[],
  pick?: string,
): PromptTemplate;
```

Versioned, renderable templates — no magic strings in agent code. `render`
throws on a missing `{{var}}`. `abPrompts` picks by weighted traffic split or
an explicit `name`/`name@version` variant.

---

## Sandbox (`sandbox/`)

```ts
function runJavascriptTool(opts?: { timeoutMs?: number; maxOutputChars?: number } = {}): Tool;
// defaults: timeoutMs 2000, maxOutputChars 4000

function matchGlob(pattern: string, path: string): boolean;

interface SandboxOptions {
  root?: string;            // workspace root; defaults to cwd
  timeoutMs?: number;       // default 30000
  maxOutputChars?: number;   // default 12000
  env?: Record<string, string>; // merged over the scrubbed base
}

interface SandboxBackend {
  readonly root: string;
  tools(): Tool[];
}

function createSandboxBackend(opts?: SandboxOptions): SandboxBackend;
```

- `runJavascriptTool` — sandboxed JavaScript via `node:vm`, fresh frozen
  context per call (Math/JSON only). Completion value becomes the result;
  output must be JSON-serializable or a string. Timeout raises
  `SANDBOX_TIMEOUT` (retryable); other failures `SANDBOX_ERROR`. A speed bump,
  not a security boundary.
- `createSandboxBackend` — a rooted workspace backend whose `tools()` are
  `ls`, `read_file`, `write_file`, `edit_file` (filesystem tools),
  `glob`, `grep`, and `execute` (shell, cwd jail + scrubbed env — only
  `PATH/HOME/LANG/LC_ALL/TZ/TERM/NO_COLOR` survive, plus your `env`).
  Timeouts raise retryable `SANDBOX_TIMEOUT`; output beyond the 4MB buffer
  raises `SANDBOX_OUTPUT_TOO_LARGE`; oversized (but under-buffer) output
  spills to `.spill-N.txt` with a read-back pointer.

---

## RAG (`rag/`)

```ts
interface ScoredDoc {
  id: string;
  text: string;
  score: number;
  meta?: Record<string, unknown>;
}

interface Retriever {
  readonly id: string;
  search(query: string, k?: number): Promise<ScoredDoc[]>;
}

class InMemoryRetriever implements Retriever { // id "tfidf"
  constructor(docs: { id: string; text: string; meta?: Record<string, unknown> }[]);
  async search(query: string, k = 4): Promise<ScoredDoc[]>;
}

function retrievalTool(retriever: Retriever, opts?: { k?: number; name?: string } = {}): Tool;

function sparqlRetriever(
  store: { match: (s: null, p: null, o: null) => Promise<{ subject: string; predicate: string; object: string }[]> },
  opts?: { id?: string },
): Retriever;
```

No bundled vector DB (local-first): `InMemoryRetriever` is a dependency-free
TF-IDF retriever over static documents; `retrievalTool` (named `retrieve` by
default, k in 1..10) exposes any `Retriever` to agents; `sparqlRetriever`
renders closure triples as scored text over any `GraphStore.match`.

---

## Evals (`evals/`)

```ts
interface EvalExpect {
  contains?: string[];
  toolUsed?: string[];
  noStructuredError?: boolean;
}

interface EvalCase { id: string; input: string; expect?: EvalExpect; }
interface EvalScore { id: string; pass: boolean; detail: string; usage: TokenUsage; }

function loadJsonl(path: string): EvalCase[];
async function runEval(
  cases: EvalCase[],
  makeAgent: (c: EvalCase) => Agent,
): Promise<{ scores: EvalScore[]; passRate: number }>;
function printReport(scores: EvalScore[], passRate: number): void;
```

JSONL datasets + scoring + pass-rate reporter. Offline by default (stub models).

---

## Semantic graph (`semantic/`)

### Store contract

```ts
interface Quad {
  subject: string;
  predicate: string;
  object: string;
  graph?: string;
  /** Provenance: who vouched, source URI, valid-until. Required on agent writes. */
  source?: string;
  derivedFrom?: string;
  rule?: string;
}

interface ShapeReport {
  valid: boolean;
  violations: { message: string; node?: string; path?: string }[];
}

interface GraphStore {
  add(quads: Quad[]): Promise<void>;
  /** Pattern match; null/undefined = wildcard (including the graph slot).
      Queries the entailment closure; a `graph` value selects ONE named graph. */
  match(s?: string | null, p?: string | null, o?: string | null, g?: string | null): Promise<Quad[]>;
  validate(quads: Quad[]): Promise<ShapeReport>;
  /** Proof chain for an entailed fact: premises + rule id. Null for explicit facts. */
  why(quad: Quad): Promise<{ premises: Quad[]; rule: string } | null>;
}
```

### `InMemoryGraphStore`

```ts
interface PropertyShape {
  path: string;
  minCount?: number;
  maxCount?: number;
  datatype?: string;
  nodeKind?: "iri" | "literal";
  hasValue?: string;
}

interface NodeShape { targetClass: string; properties: PropertyShape[]; }
interface TriplePattern { subject: string; predicate: string; object: string; }
interface GraphRule { id: string; version?: string; where: TriplePattern[]; construct: TriplePattern[]; }
interface StoreOptions { shapes?: NodeShape[]; rules?: GraphRule[]; }

class InMemoryGraphStore implements GraphStore {
  constructor(opts?: StoreOptions);
  addShapes(...shapes: NodeShape[]): this;
  addRules(...rules: GraphRule[]): this;
  /** Registered SHACL shapes (carried to new stores by TBox migrations). */
  ontologyShapes(): NodeShape[];
  ontologyRules(): GraphRule[];
  async add(quads: Quad[]): Promise<void>;       // SHACL + provenance + functional checks
  async match(s?, p?, o?, g?): Promise<Quad[]>;
  async validate(quads: Quad[]): Promise<ShapeReport>;
  async why(quad: Quad): Promise<{ premises: Quad[]; rule: string } | null>;
  /** SHACL-subset validation over a quad set (pure — no entailment). */
  checkShapes(quads: Quad[]): ShapeReport;
}
```

Entailment runs to fixpoint over this rule set (each entailed quad records
premises + rule id for `why()`):

- `RDFS-SUB-TRANS` — subclass transitivity (`a rdfs:subClassOf c` from chains)
- `RDFS-TYPE` — type inheritance through `rdfs:subClassOf`
- `RDFS-SUBPROP-TRANS` / `RDFS-SUBPROP` — subproperty chains and uses
- `RDFS-DOMAIN` / `RDFS-RANGE` — domain/range typing of property uses
- `OWL-TRANSITIVE` — transitive property composition
- `OWL-SYMMETRIC` — symmetric property reversal
- `OWL-INVERSE` — `owl:inverseOf` in both directions
- `<ruleId>@<version>` — each registered `GraphRule` (version-tagged)

Writes without a `source` fail validation (provenance is mandatory); functional
property conflicts and SHACL violations roll the batch back atomically.
Quad identity includes the named graph.

### SPARQL subset

```ts
interface SparqlSelect { variables: string[]; patterns: TriplePattern[]; }

function parseSelect(query: string): SparqlSelect;
async function executeSelect(store: GraphStore, query: string): Promise<Record<string, string>[]>;
function matchPatterns(patterns: TriplePattern[], quads: Quad[]): PatternBinding[];
function instantiate(term: string, vars: Map<string, string>): string;
```

`parseSelect` handles `SELECT ?vars WHERE { s p o . ... }` — terms are
`?vars`, `<iris>`, prefixed names, and literals (`^^datatype`/`@lang` ok);
anything else throws. This is a documented subset, not full SPARQL.

### Agent tools

```ts
function sparqlSelectTool(store: GraphStore): Tool;   // "sparql_select"
function ontologyLookupTool(store: GraphStore): Tool;  // "ontology_lookup"
function graphAddTool(store: GraphStore): Tool;        // "graph_add"
function claimCheckTool(store: GraphStore): Tool;      // "claim_check"
```

- `sparql_select` — read-only SELECT over the closure (query 1–4000 chars).
- `ontology_lookup` — find classes/properties/labels instead of inventing predicates.
- `graph_add` — autonomous validated write (1–50 quads, provenance required);
  violations return as tool results (repair feedback), not throws. Sets an
  `artifact` with the committed count.
- `claim_check` — anti-hallucination primitive: verdict `grounded | unknown |
  contradicted` with proof (contradiction needs a functional property clash).

### `GraphMemory`

```ts
class GraphMemory implements Memory {
  constructor(store: GraphStore, inner: Memory, opts?: { facts?: number } = {});
  async load(): Promise<ChatMessage[]>;   // inner messages + top-k relevant closure facts
  async save(messages: ChatMessage[]): Promise<void>; // delegates (extraction stays model-driven)
  async clear(): Promise<void>;
  async exportState(): Promise<{ quads: Quad[] }>;              // for snapshots
  async importState(state: { quads: Quad[] }): Promise<void>;    // facts become explicit again
}
```

Session memory + long-term store in one: `load()` appends the top-k closure
facts relevant to the latest user turn (keyword overlap, default 8) as a
knowledge message.

### Remote SPARQL (`remote-sparql.ts`)

```ts
type SparqlAuth =
  | { type: "basic"; user: string; password: string }
  | { type: "bearer"; token: string }
  | { type: "header"; name: string; value: string };

interface RemoteSparqlOptions {
  endpoint: string;            // e.g. a public query service URL
  updateEndpoint?: string;    // defaults to endpoint
  auth?: SparqlAuth;
  prefixes?: Record<string, string>;
  headers?: Record<string, string>;
  fetchFn?: typeof fetch;
  timeoutMs?: number;         // default 30000
  maxRetries?: number;        // default 2 (429/5xx/network, Retry-After honored)
}

type SparqlRow = Record<string, string>;

class RemoteSparqlAdapter {
  constructor(opts: RemoteSparqlOptions);
  withPrefixes(query: string): string;
  async select(query: string): Promise<SparqlRow[]>;
  async update(query: string): Promise<void>; // SPARQL 1.1 UPDATE — opt-in
}

function remoteSparqlTool(adapter: RemoteSparqlAdapter, opts?: { allowUpdate?: boolean } = {}): Tool;
```

Talk to real SPARQL endpoints (Fuseki, GraphDB, Virtuoso, public query
services) with auth, pinned prefixes (rdf/rdfs/owl/xsd/skos/dct/prov/schema by
default), a descriptive `User-Agent` (public endpoints 403 without one), and
built-in retries for rate limits. SELECT responses must be
`sparql-results+json`. The tool is `remote_sparql_select` (read-only) or
`remote_sparql` (SELECT + opt-in UPDATE) depending on `allowUpdate`. Errors are
`AgentError(REMOTE_SPARQL)` — retryable for network/429/5xx.

### Entity linking (`entity-linking.ts`)

```ts
interface LinkedEntity {
  iri: string;            // canonical (sameAs-group representative)
  label?: string;
  score: number;
  sameAsGroup: string[];
}

class EntityLinker {
  constructor(store: GraphStore);
  async link(mention: string, k?: number): Promise<LinkedEntity[]>; // k default 5
}

function linkEntityTool(linker: EntityLinker): Tool; // "link_entity"

interface MappingProposal {
  id: string;
  /** Source-side term: usually a raw column header / mention string. */
  from: string;
  /** Proposed target: a canonical ontology IRI. */
  to: string;
  /** Provenance for the eventual commit (required by GraphStore.validate). */
  source: string;
  status: "pending" | "approved" | "rejected";
  /** True when autonomy committed it on proposal (no human turn). */
  committedByAutonomy?: boolean;
  proposedAt: string;
}

type MappingDecision = "approved" | "rejected";

interface MappingProposalsOptions {
  /** Proposals commit IMMEDIATELY on proposal. DEFAULT ON. */
  autonomy?: boolean;
  /** Store to commit against; without one proposals queue as pending. */
  store?: GraphStore;
}

class MappingProposals {
  constructor(opts?: MappingProposalsOptions);
  async propose(from: string, to: string, source: string): Promise<MappingProposal>;
  async decide(store: GraphStore, id: string, decision: MappingDecision): Promise<MappingProposal | undefined>;
}

function proposeMappingTool(proposals: MappingProposals): Tool; // "propose_mapping"

// JSON-LD I/O (named graphs round-trip; provenance does not)
function quadsToJsonLd(quads: Quad[], prefixes?: Record<string, string>): Record<string, unknown>;
function jsonldToQuads(
  doc: { "@graph"?: unknown[] } & Record<string, unknown>,
  prefixes?: Record<string, string>,
): { subject: string; predicate: string; object: string; graph?: string }[];
```

Case-insensitive label index (`rdfs:label` / `skos:prefLabel` /
`skos:altLabel`) over the closure, canonicalized through `owl:sameAs` groups.
Autonomy-first: `propose_mapping` commits on proposal by default (as a
provenance-carrying `skos:altLabel`); `new MappingProposals({ autonomy: false })`
queues proposals for `decide()`, or gate the tool with the HITL middleware.
Named graphs round-trip through JSON-LD; provenance does not.

### Ontology evolution (`ontology-evolution.ts`)

```ts
interface ConformanceCheck {
  id: string;
  ask?: string;        // SPARQL-subset SELECT; row count in [minRows, maxRows]
  minRows?: number;    // default 1 (ask-style); {0,0} asserts absence
  maxRows?: number;
  shape?: NodeShape;   // extra SHACL shape the closure must satisfy
  reason?: string;
}
interface ConformanceResult { id: string; passed: boolean; detail: string; }
interface ConformanceReport { passed: boolean; results: ConformanceResult[]; }

async function runConformance(store: InMemoryGraphStore, battery: ConformanceCheck[]): Promise<ConformanceReport>;

interface TBoxMigrationSpec {
  from: string;
  to: string;
  added?: Quad[];
  removed?: { subject: string; predicate: string; object: string }[];
  renames?: { from: string; to: string }[];
  deprecations?: { term: string; replacement?: string; reason: string }[];
  shapes?: NodeShape[];
  rules?: StoreOptions["rules"];
}

interface MigrationReport {
  from: string; to: string;
  status: "migrated" | "aborted" | "failed";
  addedQuads: number; removedQuads: number; rewrittenQuads: number;
  tboxGraph?: string;
  deprecatedWarnings: string[]; errors: string[];
  preBattery?: ConformanceReport; postBattery?: ConformanceReport;
}

interface MigrationResult {
  report: MigrationReport;
  /** Present on success — the new-version store with a fresh closure. The old store is untouched. */
  store?: InMemoryGraphStore;
}

async function migrateTBox(
  oldStore: InMemoryGraphStore,
  spec: TBoxMigrationSpec,
  opts?: {
    battery?: ConformanceCheck[];
    factory?: (opts: StoreOptions) => InMemoryGraphStore;
    tboxGraph?: string | false;
  },
): Promise<MigrationResult>;
```

Versioned TBox migration: pre-battery (abort on failure) -> rebuild (the old
store survives; rollback is "don't adopt") -> post-battery (caller decides).
The v4 TBox axioms are written to their own named graph (configurable, `false`
opts out). Also exports the TBox predicate/vocab marker sets used to classify
axioms vs. instance facts.

### Ontology review (`ontology-review.ts`)

```ts
interface TermStat {
  term: string;
  kind: "class" | "property";
  uses: number;        // explicit ABox uses
  axiomCount: number;  // TBox axioms mentioning the term
  label?: string;
}

interface OntologyReview {
  classes: TermStat[];
  properties: TermStat[];
  undeclared: TermStat[];  // used in data with zero TBox declarations
  unused: string[];        // declared but never used
  unlabeled: string[];     // no rdfs:label / skos:prefLabel / altLabel
  missingDomain: string[];
  missingRange: string[];
  explicitQuads: number;
  closureQuads: number;
}

async function reviewOntology(store: InMemoryGraphStore): Promise<OntologyReview>;

interface ShapeInductionOptions {
  minInstances?: number; // classes with fewer instances produce no shape. Default 2.
  coverage?: number;     // property present on >= this fraction. Default 0.5.
}

async function proposeShapes(store: InMemoryGraphStore, opts?: ShapeInductionOptions): Promise<NodeShape[]>;

async function adoptShapes(
  store: InMemoryGraphStore,
  shapes: NodeShape[],
): Promise<{ adopted: NodeShape[]; rejected: { shape: NodeShape; violations: string[] }[] }>;

function reviewOntologyTool(store: InMemoryGraphStore): Tool;   // "review_ontology"
function proposeShapesTool(store: InMemoryGraphStore, opts?): Tool; // "propose_shapes"
```

`reviewOntology` audits what the ontology IS (inventory + gaps, pure read).
`proposeShapes` induces candidate SHACL shapes from instance data — PROPOSALS
only; shapes are builder-owned, adopted via `adoptShapes()` (each validated
against the current closure in isolation) or the next `migrateTBox({ shapes })`,
never silently.

---

## Utils (`utils/`)

```ts
async function sleep(ms: number, signal?: AbortSignal): Promise<void>;

function estimateTokens(text: string): number;

function estimateCostUsd(
  usage: { inputTokens: number; outputTokens: number },
  price: { inputPerMillion: number; outputPerMillion: number },
): number;
```

- `estimateTokens` — local, dependency-free approximation (not a BPE): counts
  word-ish runs, numbers, CJK characters, punctuation clusters; calibrated so
  `estimateTokens("abcd") === 1`. Use it for budgets and triggers, prefer
  provider usage metadata for reporting.
- `estimateCostUsd` — rough USD cost against a per-1M-token price table.

---

## Related pages

- [Python API reference](python.md) — name-for-name parity surface.
- [React hook](react.md) — `framework-agents-react` on top of `AgentClient`.
- [Errors & retry contract](errors.md) — every code, its retryable default.
- [Pinned semantics](../reference/pinned-semantics.md) — behaviors the loop fixes in stone.