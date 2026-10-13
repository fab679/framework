# Python API reference (`framework_agents`)

Every public symbol of the Python package, generated from
`framework_agents.__all__` and organized by module. Import from the package
root or the submodules:

```python
from framework_agents import Agent, AgentOptions, define_tool, init_chat_model
from framework_agents.semantic import InMemoryGraphStore
from framework_agents.middleware import human_in_the_loop
```

Requirement: Python >= 3.11, `pydantic` + `httpx`. The port is name-for-name
parity with the TypeScript package except where the naming-differences table
below says otherwise. The shared error contract is on
[Errors & retry contract](errors.md).

## Reading Python async in this framework

- **`async def` methods need `await`** — and `await` only works inside
  `async def`; run agents under `asyncio.run(...)`.
- **Streams are async iterators**: `async for event in agent.stream(...)`.
- **The result is `stream.result`**: Python async generators cannot carry a
  return value like the TS generator does, so `agent.stream()` returns an
  `AgentStream` that captures the finished `AgentResult` at the moment the
  `done` event is observed. After the loop (or via `collect_stream`) read it
  from `stream.result`. `agent.run()` returns the `AgentResult` directly.

---

## Agent (`agent.py`)

### `AgentOptions`

```python
@dataclass
class AgentOptions:
    model: ModelProvider
    name: str = "agent"
    description: str = ""
    system_prompt: str = "You are a helpful agent. Use tools when needed. Be concise."
    tools: list[Tool] = field(default_factory=list)
    memory: Optional[Memory] = None
    max_steps: int = 6                  # DEFAULT_MAX_STEPS
    max_tool_calls: Optional[int] = None
    budgets: Optional[TokenBudgets] = None
    response_format: Optional[ResponseFormat] = None
    events: Optional[AgentEvents] = None
    initial_messages: list[ChatMessage] = field(default_factory=list)
    # Middleware hooks; contributed tools are PREPENDED, dedupe by name (first wins).
    middleware: list[Any] = field(default_factory=list)
    # Per-run state shared by reference with hooks (never sent to the model);
    # a run invocation_state kwarg overrides this.
    invocation_state: Optional[dict[str, Any]] = None
    # Agent key-value state: AgentState or a plain dict (validated at construction).
    state: AgentState | dict[str, Any] | None = None
    # Persist state + history across restarts. Restored on first run (BEFORE
    # initial_messages seeding — the session wins over the seed), saved after
    # each run.
    session_manager: Optional[SessionManager] = None
    output_version: Literal["v0", "v1"] = "v0"  # v1 also stores thinking as reasoning blocks
```

Field names mirror the TS `AgentOptions` in snake_case
(`system_prompt`/`max_steps`/`max_tool_calls`/`response_format`/
`initial_messages`/`session_manager`/`output_version`).

### `TokenBudgets`

```python
@dataclass
class TokenBudgets:
    max_tokens: Optional[int] = None
    max_cost_usd: Optional[float] = None
    prices: Optional[ModelPrice] = None   # price override for max_cost_usd
    deadline_ms: Optional[float] = None
```

Mirrors the TS `TokenBudgets` names. Reasoning tokens are a SUBSET of
`output_tokens` — never added on top.

### `ResponseFormat` / strategies

```python
@dataclass
class ResponseFormat:
    schema: Type[BaseModel]      # a pydantic model is the validated arg shape
    name: str = "structured_response"
    strategy: Literal["auto", "provider", "tool"] = "auto"
    max_attempts: int = 2

def provider_strategy(schema: Type[BaseModel], name: str = "structured_response") -> ResponseFormat
def tool_strategy(schema: Type[BaseModel], name: str = "structured_response") -> ResponseFormat
```

`auto` resolves by provider capability (`capabilities["structured_output"]`);
the tool strategy injects a synthetic `<name>` tool whose validation failures
return repair feedback; the provider strategy reads the JSON text back through
`schema.model_validate_json`.

### `AgentEvents`

```python
@dataclass
class AgentEvents:
    on_step: Optional[Callable[[int], None]] = None
    on_token: Optional[Callable[[str], None]] = None
    on_thinking: Optional[Callable[[str], None]] = None
    on_tool_call: Optional[Callable[[str, Any], None]] = None
    on_tool_result: Optional[Callable[[str, Any], None]] = None
    on_done: Optional[Callable[[str], None]] = None
```

### `AgentResult`

```python
@dataclass
class AgentResult:
    text: str
    messages: list[ChatMessage]
    tool_calls: list[ExecutedToolCall]
    usage: TokenUsage
    structured_response: Optional[Any] = None
    invocation_state: Optional[dict[str, Any]] = None
```

### `Agent`

```python
class Agent:
    def __init__(self, options: AgentOptions) -> None: ...

    @property
    def name(self) -> str: ...
    @property
    def messages(self) -> list[ChatMessage]: ...      # history from the last run
    @property
    def state(self) -> AgentState: ...               # key-value state (never sent to the model)
    @property
    def session_id(self) -> str: ...                 # manager's id, or generated per-instance
    @property
    def timeline(self) -> list[dict[str, Any]]: ...  # runs/edits/branches/resumes (append-only)

    def with_tools(self, *extra: Tool) -> "Agent": ...
    async def edit_message(self, id_or_index: str | int, content: Any) -> ChatMessage: ...
    async def branch(self, label: str, overrides: dict[str, Any] | None = None) -> "Agent": ...
    async def pause(self, thread_id: str, checkpointer: Any) -> dict[str, Any]: ...
    def take_snapshot(self, opts: dict[str, Any] | None = None) -> Snapshot: ...
    async def load_snapshot(self, snapshot: Snapshot) -> None: ...
    async def resume(
        self,
        thread_id: str,
        decisions: Optional[dict[str, Any]] = None,
        checkpointer: Any = None,
        invocation_state: Optional[dict[str, Any]] = None,
    ) -> AgentResult: ...
    def stream(
        self,
        input_text: str | list[Any],
        invocation_state: Optional[dict[str, Any]] = None,
        checkpointer: Any = None,
        thread_id: Optional[str] = None,
    ) -> AsyncIterator[StreamEvent]: ...             # AgentStream; .result after done
    async def run(
        self,
        input_text: str | list[Any],
        invocation_state: Optional[dict[str, Any]] = None,
        checkpointer: Any = None,
        thread_id: Optional[str] = None,
    ) -> AgentResult: ...

    # Blocking one-liners for scripts AND notebooks — they detect the running
    # loop: scripts get asyncio.run on the spot; notebooks (whose loop is
    # already running) execute on a private loop in a worker thread.
    def run_sync(self, input_text: str, **kwargs: Any) -> AgentResult: ...
    def stream_sync(self, input_text: str, **kwargs: Any) -> Generator[StreamEvent, None, None]: ...
```

Semantics match the TS loop (see [Pinned semantics](../reference/pinned-semantics.md)):
budgets fire after every model call on cumulative usage; memory save is
clear-then-save; unknown tools / invalid args become model-visible feedback;
`max_steps` exhaustion stores a synthesized assistant message; cost is only
reported when a price is known.

---

## Types (`types.py`)

### Message models

```python
Role = Literal["system", "user", "assistant", "tool"]

class ChatMessage(BaseModel):
    id: str | None = None
    role: Role
    content: str | list[Any]            # string or ContentBlock models/dicts
    name: str | None = None
    tool_call_id: str | None = None      # alias toolCallId
    tool_name: str | None = None         # alias toolName
    pinned: bool | None = None
    def blocks(self) -> list[BaseModel]: ...
```

`blocks()` returns the parsed content blocks (cached per instance;
string content -> `[]`). Content-block classes: `TextBlock`, `ReasoningBlock`,
`ImageBlock`, `AudioBlock`, `FileBlock`, `ToolCallBlock`, `NonStandardBlock`
(their union is the `ContentBlock` type). `parse_content_block(raw)` validates
one block dict and raises on unknown shapes.

### Usage models

```python
class TokenUsage(BaseModel):
    input_tokens: int = 0              # alias inputTokens
    output_tokens: int = 0              # alias outputTokens
    reasoning_tokens: int | None = None  # SUBSET of output_tokens — report, never add
    cost_usd: float | None = None         # set only when a price is known
    def __add__(self, other: "TokenUsage") -> "TokenUsage": ...

class ToolCall(BaseModel): id: str; name: str; args: Any = None

class ExecutedToolCall(BaseModel): id: str; name: str; args: Any = None; output: Any = None
```

### Message factories and helpers

```python
def system(content: str | list[Any]) -> ChatMessage
def user(content: str | list[Any], id: str | None = None, name: str | None = None) -> ChatMessage
def assistant(content: str | list[Any]) -> ChatMessage
def tool_result(tool_call_id: str, tool_name: str, content: str | list[Any]) -> ChatMessage

def serialize_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]
def deserialize_messages(raw: list[dict[str, Any]]) -> list[ChatMessage]   # raises on bad shape

def text_of(content: str | list[Any]) -> str
def wire_text_of(content: str | list[Any]) -> str   # reasoning blocks DROPPED for providers
def has_images(content: str | list[Any]) -> bool
def parse_content_block(raw: Any) -> BaseModel
```

---

## Tools (`tools.py`)

```python
@dataclass
class ToolResult:
    content: Any            # model-visible content
    artifact: Any = None     # downstream-only (never sent to the model)

@dataclass
class Tool:
    name: str
    description: str
    schema: Type[BaseModel]  # pydantic model = the validated arg shape
    execute: Callable[[Any, ToolContext | None], Awaitable[Any]]
    to_result: Callable[[Any], Awaitable[ToolResult]] = _default_to_result

@dataclass
class ToolContext:
    agent_name: str = "agent"
    state: AgentState = field(default_factory=AgentState)
    invocation_state: dict[str, Any] = field(default_factory=dict)

def define_tool(
    *, name: str, description: str, schema: Type[BaseModel],
    execute: Callable[[Any, ToolContext | None], Awaitable[Any]],
    to_result: Optional[Callable[[Any], Awaitable[ToolResult]]] = None,
) -> Tool

def parameters_schema(tool: Tool) -> dict[str, Any] | None

class ToolRegistry:
    def register(self, tool: Tool) -> "ToolRegistry": ...
    def get(self, name: str) -> Optional[Tool]: ...
    def list(self) -> list[Tool]: ...
    async def call(self, name: str, raw_args: Any, ctx: ToolContext | None = None) -> Any: ...
```

`parameters_schema` returns the JSON Schema for the wire (without it, a tool is
advertised zero-arg). `ToolRegistry.call` raises `UNKNOWN_TOOL` /
`TOOL_ARG_INVALID`. Pydantic replaces zod throughout: `schema` is a
`BaseModel` subclass, and rows validate through `model_validate`.

---

## Streaming (`streaming.py`)

```python
class StreamEvent(TypedDict, total=False):
    type: str    # "step" | "token" | "thinking" | "tool_call" | "tool_result" | "done"
    step: int
    delta: str
    name: str
    args: Any
    output: Any
    text: str

class AgentStream:
    def __aiter__(self) -> "AgentStream": ...
    async def __anext__(self) -> StreamEvent: ...
    async def aclose(self) -> None: ...
    async def athrow(self, typ, val=None, tb=None) -> Any: ...
    @property
    def result(self) -> Any: ...   # the AgentResult captured at the done event

async def collect_stream(stream: AsyncIterator[StreamEvent]) -> dict[str, Any]
```

`collect_stream` returns `{"events": [...], "result": AgentResult}` and raises
`RUN_INCOMPLETE` when the stream ended without a result. The per-stream capture
means concurrent streams on ONE agent each keep their own result.

---

## Models (`models/` + `config.py`)

### Provider protocol and completions

```python
@dataclass
class ToolCallRequest: id: str; name: str; args: Any = None

@dataclass
class ModelCompletion:
    text: str = ""
    thinking: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None

@dataclass
class ToolWire:
    name: str
    description: str
    parameters_schema: dict[str, Any] | None = None

@dataclass
class ResponseFormatWire: name: str; schema: dict[str, Any]; strict: bool | None = None

@dataclass
class CompleteOptions:
    messages: list[ChatMessage]
    tools: list[ToolWire] = field(default_factory=list)
    response_format: ResponseFormatWire | None = None
    on_token: Callable[[str], None] | None = None
    on_thinking: Callable[[str], None] | None = None
    signal: Any | None = None   # httpx-compatible abort handle

@runtime_checkable
class ModelProvider(Protocol):
    id: str
    capabilities: dict[str, bool]
    async def complete(self, options: CompleteOptions) -> ModelCompletion: ...

class MockModelProvider:
    id: str = "mock"
    capabilities = {"structured_output": False, "streaming": False}
    def __init__(self) -> None: ...
    async def complete(self, options: CompleteOptions) -> ModelCompletion: ...

class ScriptedModelProvider:
    id: str = "scripted"
    capabilities = {"structured_output": False, "streaming": False}
    def __init__(self, completions: list[ModelCompletion]) -> None: ...
    async def complete(self, options: CompleteOptions) -> ModelCompletion: ...
```

`MockModelProvider` returns deterministic responses (and fires `on_token`);
`ScriptedModelProvider` returns canned completions in order (last repeats).

### `init_chat_model`

```python
def init_chat_model(
    spec: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    max_tokens: int | None = None,
    stream: bool = False,
    thinking_budget: int | None = None,
) -> ModelProvider
```

Same `provider:model` prefixes as TS: `together:` (default for bare ids),
`openai:`, `anthropic:`, `ollama:`, `custom:` (needs `base_url` + `api_key`).

| Prefix | Provider | Key |
|---|---|---|
| `together:<id>` / bare `<id>` | TogetherAI (OpenAI-compatible) | `TOGETHER_API_KEY` |
| `openai:<id>` | OpenAI | `OPENAI_API_KEY` |
| `anthropic:<id>` | Anthropic native | `ANTHROPIC_API_KEY` |
| `ollama:<id>` | local Ollama | none |
| `custom:<id>` | any OpenAI-compatible | `api_key` arg |

### Providers

```python
@dataclass
class OpenAICompatibleOptions:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    max_tokens: int | None = None
    stream: bool = False
    fetch_fn: Callable[..., Any] | None = None   # httpx-client factory for tests

class OpenAICompatibleProvider:
    capabilities = {"structured_output": True, "streaming": True}
    def __init__(self, opts: OpenAICompatibleOptions) -> None: ...   # id: openai-compatible:<model>
    async def complete(self, options: CompleteOptions) -> ModelCompletion: ...

def provider_from_env(active: tuple[str, str, str, str], extra: Any = None) -> OpenAICompatibleProvider

@dataclass
class AnthropicOptions:
    api_key: str
    model: str
    base_url: str | None = None     # default https://api.anthropic.com
    max_tokens: int = 1024
    temperature: float | None = None
    thinking_budget: int | None = None  # extended thinking; must be < max_tokens
    stream: bool = False
    fetch_fn: Callable[[], Any] | None = None

class AnthropicProvider:
    capabilities = {"structured_output": False, "streaming": True}
    def __init__(self, opts: AnthropicOptions) -> None: ...          # id: anthropic:<model>
    async def complete(self, options: CompleteOptions) -> ModelCompletion: ...

@dataclass
class OllamaOptions:
    model: str
    base_url: Optional[str] = None   # default http://localhost:11434/v1
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = None
    fetch_fn: Optional[Callable[[], Any]] = None

class OllamaProvider(OpenAICompatibleProvider):
    capabilities = {"structured_output": False, "streaming": True}
    def __init__(self, opts: OllamaOptions) -> None: ...             # id: ollama:<model>

def ollama(model: str, **kwargs: Any) -> OllamaProvider
```

Pinned provider semantics (see the Parity page for the deviations list):
union field parsing for thinking (`reasoning` / `reasoning_content`), strict
usage parsing in streams, loud `MODEL_ERROR` on `finish_reason="length"` with
zero usable content, non-repeating streaming tool-call ids, and 429/5xx ->
retryable / other HTTP errors -> non-retryable.

### Capabilities (`models/capabilities.py`)

```python
def supports_vision(model_id: str) -> bool
def assert_can_handle(model_id: str, needs_vision: bool) -> None  # MODEL_CAPABILITY raise
```

---

## Config (`config.py`)

```python
@dataclass
class Config:
    together_api_key: str
    together_base_url: str
    together_model: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    anthropic_api_key: str
    anthropic_base_url: str
    anthropic_model: str
    ollama_base_url: str
    ollama_model: str
    @property
    def active(self) -> tuple[str, str, str, str]: ...  # (source, base_url, api_key, model)

def load_config() -> Config
def parse_env_file(text: str) -> dict[str, str]
```

Walk-up `.env` loading (cwd, then 3 parents; `FRAMEWORK_ENV_PATH` overrides),
TogetherAI preferred when its key is set — the same defaults as TS
`loadConfig`. `parse_env_file` supports `export KEY=v`, strips unquoted
trailing comments, and lets the LAST duplicate in one file win (real process
env always wins — values are applied with `setdefault`).

---

## Pricing (`pricing.py`)

```python
class ModelPrice(BaseModel):
    input_per_million: float
    output_per_million: float

def price_for(model_id: str) -> Optional[ModelPrice]
def usage_cost(usage, model_id: str, override: Optional[ModelPrice] = None) -> Optional[float]
```

Same researched price table as TS; `ollama:` ids are free (they win over name
collisions); unknown models return `None` — never a fake zero.

---

## Memory (`memory.py`, `memory_compaction.py`)

```python
class Memory(Protocol):
    async def load(self) -> list[ChatMessage]: ...
    async def save(self, messages: list[ChatMessage]) -> None: ...  # APPENDS (windows)
    async def clear(self) -> None: ...

class InMemoryMemory:
    def __init__(self, max_messages: int = 50) -> None: ...

class FileMemory:
    def __init__(self, directory: str, max_messages: int = 50) -> None: ...
    # <dir>/memory.json, atomic write-then-rename, corrupt raises MEMORY_CORRUPT

class CompactedMemory:
    def __init__(self, inner: Memory, max_messages: int = 40,
                 summarize: SummarizeFn | None = None) -> None: ...
    # load/save/clear delegate; save() compacts overflow into one summary message

def default_summarize(messages: list[ChatMessage]) -> str
```

Pinned: save() APPENDS (the loop does clear-then-save); corrupted files fail
loudly (`MEMORY_CORRUPT`) instead of wiping what's on disk; tmp files are
unique per flush.

---

## Conversation managers (`conversation.py`)

```python
class ConversationManager(Protocol):
    name: str
    removed_count: int
    async def apply(self, messages: list[ChatMessage]) -> list[ChatMessage]: ...
    async def reduce(self, messages: list[ChatMessage]) -> list[ChatMessage]: ...

def estimate_tokens(text: str) -> int
def message_tokens(m: ChatMessage) -> int
def history_tokens(messages: list[ChatMessage]) -> int
def trim_dangling(messages: list[ChatMessage]) -> list[ChatMessage]

class NullConversationManager: ...                     # keep everything

class SlidingWindowConversationManager:
    def __init__(self, window_size: int = 40, pin_first: int = 0,
                 per_turn: bool | int = False) -> None: ...
    async def before_model(self, messages: list[ChatMessage], step: int) -> list[ChatMessage]: ...

class SummarizingConversationManager:
    def __init__(self, summary_ratio: float = 0.3, preserve_recent: int = 10,
                 pin_first: int = 0, summarizer: Summarizer | None = None,
                 model: ModelProvider | None = None,
                 summary_prefix: str = "Summary of earlier conversation:") -> None: ...
    last_summary: str | None   # set when the last apply() summarized
```

Pinned: sliding-window keeps original message order; `before_model` manages
per turn (default: after-run only); dangling tool heads are trimmed; pinned
messages always survive.

---

## Sessions (`sessions.py`)

```python
class Storage(Protocol):
    async def read(self, key: str) -> str | None: ...
    async def write(self, key: str, value: str) -> None: ...
    async def delete_prefix(self, prefix: str) -> None: ...
    async def list(self, prefix: str) -> list[str]: ...

class InMemoryStorage: ...
class LocalFileStorage:
    def __init__(self, root: str) -> None: ...   # traversal keys -> STORAGE_KEY_INVALID

class SnapshotData(BaseModel):
    messages: list[ChatMessage] | None = None
    state: dict[str, Any] | None = None
    system_prompt: str | None = None            # alias systemPrompt
    def dump(self) -> dict[str, Any]: ...

def select_snapshot_fields(
    full: SnapshotData,
    preset: Literal["session"] | None = None,
    include: list[SnapshotField] | None = None,
    exclude: list[SnapshotField] | None = None,
) -> SnapshotData

@dataclass
class Snapshot:
    id: str
    created_at: str
    data: SnapshotData
    app_data: dict[str, Any] | None = None

class SessionManager:
    def __init__(self, storage: Storage, session_id: str | None = None,
                 agent_id: str = "default") -> None: ...
    async def save_latest(self, data: SnapshotData) -> None: ...
    async def load_latest(self) -> SnapshotData | None: ...
    async def save_snapshot(self, data: SnapshotData, app_data: dict[str, Any] | None = None) -> str: ...
    async def list_snapshot_ids(self) -> list[str]: ...
    async def restore_snapshot(self, id: str) -> Snapshot | None: ...
    async def delete_session(self) -> None: ...
```

Deviation worth knowing: without a preset, selection starts from the EMPTY
base (`include` picks exactly those fields) — bundling a preset is the TS
behavior.

---

## Checkpoint (`checkpoint.py`)

```python
@dataclass
class PendingCall:
    interrupt_id: str
    tool: str
    args: Any = None

@dataclass
class ThreadCheckpoint:
    thread_id: str
    saved_at: str
    messages: list[Any] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    pending: list[PendingCall] = field(default_factory=list)

class Checkpointer(Protocol):
    async def save(self, thread_id: str, checkpoint: ThreadCheckpoint) -> None: ...
    async def load(self, thread_id: str) -> ThreadCheckpoint | None: ...
    async def delete(self, thread_id: str) -> None: ...

class FileCheckpointer(Checkpointer):
    def __init__(self, root: str) -> None: ...   # <root>/threads/<threadId>/checkpoint.json

async def with_file_lock(lock_path: Path | str, fn: Callable[[], ...],
                         stale_ms: float = 30000) -> Any
```

Python-only hard codes: strict serialization raises `CHECKPOINT_SERIALIZABLE`
(non-JSON values never resume as reprs), a failed write raises
`CHECKPOINT_WRITE` (retryable), lock failures raise `LOCK_ERROR`. `threadId`
is a single safe directory name (`THREAD_ID_INVALID` otherwise).

---

## Middleware (`middleware.py`)

Hooks (all optional, sync or async): `before_model`, `wrap_model`,
`after_model`, `before_tool`, `around_tool`, `after_tool` (+ `name`, `tools`).
Order is pinned: before hooks in registration order, after hooks REVERSE,
wrap hooks nested first-registered-outermost. `run_before_model`,
`run_after_model`, `compose_model_call`, `run_before_tool`,
`compose_tool_call`, `run_after_tool` implement the chains.

```python
@dataclass
class HookContext:
    step: int
    invocation_state: dict[str, Any] = field(default_factory=dict)

@dataclass
class ModelRequest:            # CompleteOptions-shaped request the hooks see
    messages: list[ChatMessage]
    tools: list[ToolWire] = field(default_factory=list)
    response_format: ResponseFormatWire | None = None

@dataclass
class ToolCallRef: name: str; args: Any = None

class ToolRejection(Exception):
    def __init__(self, feedback: str) -> None: ...   # .feedback is model-visible

class HitlInterrupt(Exception):
    def __init__(self, interrupt_id: str, tool: str, args: Any = None) -> None: ...

HITL_DECISIONS_KEY = "__hitl_decisions"

def hitl_key(tool: str, args: Any) -> str    # f"{tool}:{json.dumps(args)}"

def create_middleware(m: Any) -> Any          # identity factory

class Middleware:
    def __init__(self, name: str, *, tools=None, before_model=None, wrap_model=None,
                 after_model=None, before_tool=None, around_tool=None, after_tool=None) -> None: ...
```

### Prebuilt middlewares

```python
def human_in_the_loop(
    interrupt_on: dict[str, Any],
    decide: Optional[Callable[[dict[str, Any]], Any]] = None,
) -> Any

def model_retry(
    max_retries: int = 3, initial_delay_ms: float = 1000, backoff_factor: float = 2,
    max_delay_ms: float = 60000, jitter: bool = True,
    retry_on: RetryOn = None,   # None | callable | exception-class list
    on_failure: OnFailure = "error",   # "error" | "continue" | (err) -> str
) -> Any

def tool_retry(
    max_retries: int = 3, initial_delay_ms: float = 1000, backoff_factor: float = 2,
    max_delay_ms: float = 60000, jitter: bool = True,
    retry_on: RetryOn = None, on_failure: OnFailure = "error",
    tools: Optional[list[str]] = None,
) -> Any

def model_call_limit(max_calls: int) -> Any
def tool_call_limit(tool_name: Optional[str] = None, run_limit: Optional[int] = None,
                    exit_behavior: str = "error") -> Any

def policy_filter(
    allow: Optional[list[Any]] = None,   # exact names or regexes
    deny: Optional[list[Any]] = None,
    exit_behavior: str = "continue",     # "error" raises POLICY_DENIED
) -> Any

def pii_redact(
    pii_type: str, strategy: PIIStrategy = "redact",
    detector: Optional[PIIDetector] = None,
    apply_to_output: bool = True, apply_to_input: bool = False,
    apply_to_tool_results: bool = False,
) -> Any

def model_fallback(*fallbacks: ModelProvider) -> Any

def summarization_middleware(
    model: ModelProvider, trigger_tokens: int, keep_messages: int = 20,
    summary_prefix: str = "Prior context summary",
    summarize: Optional[Callable[[list[ChatMessage]], Any]] = None,
) -> Any

class ClearToolUsesEdit:
    def __init__(self, older_than_steps: int | None = None, keep_recent: int = 0) -> None: ...
    def apply(self, messages: list[ChatMessage], step: int = 0) -> list[ChatMessage]: ...

def context_editing_middleware(edits: Optional[list[Edit]] = None) -> Any

def llm_tool_selector_middleware(
    model: ModelProvider, max_tools: Optional[int] = None,
    always_include: Optional[list[str]] = None,
    system_prompt: Optional[str] = None,
) -> Any

def tool_emulator_middleware(model: ModelProvider, tools: Optional[list[str]] = None) -> Any

def filesystem_tools(root: str | Path) -> list[Tool]
def filesystem_middleware(root: str | Path) -> Any

def create_sub_agent_middleware(model: ModelProvider, subagents: list[dict[str, Any]]) -> Any

def todo_list() -> Any   # contributes write_todos; mw.list_todos() -> list of dicts
```

Key contracts:

- Retries are **verdict-driven**: `get_retryable()` True retries, False never
  does (budgets and tool-call limits always pass through), unknown verdicts
  only retry with an explicit `retry_on`. `AgentError` and `HitlInterrupt`
  are NEVER degraded into feedback text — `SWARM_HANDOFF` routing, HITL
  suspension, and budget exhaustion reach the caller regardless of
  `on_failure`.
- HITL decisions are read from `invocation_state["__hitl_decisions"]` by exact
  `hitl_key(tool, args)` first, then by bare tool name (resume converges even
  when the model re-issues a call with regenerated arg text).
- PII strategies: `redact` -> `[REDACTED_<TYPE>]`, `mask` keeps the first
  char + dots, `hash` is a sha256 hexdigest[:16], `block` raises a
  `ToolRejection`. Built-in detectors: `email`, `credit_card`, `ip`.
- Documented deviations from the TS built-ins (pairs stripped by
  `ClearToolUsesEdit`, feedback-string filesystem errors, etc.) are listed on
  the [parity page](../reference/parity.md).

---

## Runtime (`runtime.py`)

```python
@dataclass
class Budget:
    signal: asyncio.Event | None = None   # set = aborted
    timeout_ms: float | None = None

class AbortHandle:
    @property
    def event(self) -> asyncio.Event: ...
    @property
    def aborted(self) -> bool: ...
    def set(self) -> None: ...
    def throw_if_aborted(self) -> None: ...    # AgentError("ABORTED")

async def run_with_budget(fn: Callable[[], Awaitable[Any]], budget: Budget | None = None) -> Any
```

`AbortHandle` is the TS `AbortSignal` equivalent. When both a signal and a
timeout are given the guard races the two branches (whichever fires first
wins; the loser is cancelled). Abort maps to `AgentError(ABORTED)`
(non-retryable), timeout to `AgentError(TIMEOUT)` (retryable).

---

## Observability (`observability.py`)

```python
def supports_color() -> bool

class ConsoleEvents:
    """AgentEvents-shaped console logger (step/token/tool/done lines)."""
    def __init__(self, prefix: str = "[agent]", colors: Optional[bool] = None) -> None: ...
    def on_step(self, step: int, messages: Any = None) -> None: ...
    def on_tool_call(self, name: str, args: Any) -> None: ...
    def on_tool_result(self, name: str, output: Any) -> None: ...
    def on_done(self, text: str, tool_calls: Any = None) -> None: ...
    def on_token(self, delta: str) -> None: ...
    def on_thinking(self, delta: str) -> None: ...

def console_events(prefix: str = "[agent]", colors: Optional[bool] = None) -> ConsoleEvents
def log_error(prefix: str, err: BaseException, colors: Optional[bool] = None) -> None
```

Colors honor `NO_COLOR`/TTY by default.

---

## Tracing (`tracing.py`)

```python
@dataclass
class Span:
    trace_id: str
    span_id: str
    name: str
    started_at: str
    ended_at: Optional[str] = None
    parent_id: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)

class SpanSink(Protocol):
    def emit(self, span: Span) -> None: ...

class ConsoleSpanSink:
    def emit(self, span: Span) -> None: ...

class JsonlSpanSink:
    def __init__(self, path: str) -> None: ...   # camelCase JSONL records

class MemorySpanSink:
    def __init__(self) -> None: ...              # .spans

class TracingEvents:
    def __init__(self, sink: SpanSink, trace_id: Optional[str] = None) -> None: ...
    # on_step / on_token / on_thinking / on_tool_call / on_tool_result / on_done

def tracing_events(sink: SpanSink, trace_id: Optional[str] = None) -> TracingEvents
```

Span names: `step.start`, `model.token`, `model.thinking`, `tool.call` ->
`tool.result` (parented to the most recent `tool.call`), `run.done`.

---

## Orchestration (`orchestration.py`)

```python
OrchestrationStrategy = Literal["sequential", "parallel", "router"]

@dataclass
class OrchestrationPlan:
    strategy: OrchestrationStrategy
    agents: list[Agent] = field(default_factory=list)
    concurrency: Optional[int] = None          # parallel; default all at once
    router_model: Optional[ModelProvider] = None  # default: first agent's model

@dataclass
class OrchestratedResult:
    strategy: OrchestrationStrategy
    results: list[AgentResult]
    text: str                    # last result (sequential/router), joined (parallel)
    routed_to: Optional[int] = None

async def run_orchestrated(
    plan: OrchestrationPlan,
    input_text: str,
    invocation_state: Optional[dict[str, Any]] = None,
) -> OrchestratedResult
```

Empty plans raise `ORCHESTRATION_EMPTY`; an unknown strategy raises
`ORCHESTRATION_STRATEGY`; a bad router pick raises `ROUTER_NO_MATCH`.
`invocation_state` threads to agents whose `run` accepts it.

---

## Workflows (`workflows.py`)

```python
@dataclass
class RetryPolicy:
    attempts: int = 1
    backoff_ms: float = 0

@dataclass
class WorkflowNode:
    id: str
    run: Callable[[dict[str, Any], Optional[dict[str, Any]]], Awaitable[Any]]
    depends_on: list[str] = field(default_factory=list)
    retry: Optional[RetryPolicy] = None

@dataclass
class WorkflowOptions:
    invocation_state: Optional[dict[str, Any]] = None

async def run_workflow(
    nodes: list[WorkflowNode],
    opts: Optional[WorkflowOptions] = None,
) -> dict[str, Any]
```

Topological levels run in order; nodes within a level run concurrently;
returns id -> result. Duplicate `dependsOn` entries are ONE edge; aborts
(`AgentError` code `ABORTED`, `asyncio.CancelledError`) always rethrow.

---

## Swarm (`swarm.py`)

```python
HANDOFF_TOOL = "handoff_to_agent"

@dataclass
class Swarm:
    agents: list[Agent]
    entry_point: Optional[Any] = None   # Agent instance or name; default agents[0]
    max_handoffs: int = 20
    max_iterations: int = 20
    execution_timeout_ms: Optional[float] = None
    node_timeout_ms: Optional[float] = None
    loop_detection_window: int = 0
    min_unique_agents: Optional[int] = None

    def __post_init__(self) -> None: ...  # SWARM_EMPTY / SWARM_UNKNOWN_ENTRY
    async def run(self, task: str) -> SwarmResult
    def stream(self, task: str) -> AsyncIterator[SwarmStreamEvent]

@dataclass
class HandoffSignal:
    to: str
    message: str
    context: Optional[dict[str, Any]] = None

def handoff_signal(to: str, message: str, context: Optional[dict[str, Any]] = None) -> AgentError
def read_handoff_signal(err: BaseException) -> Optional[HandoffSignal]

@dataclass
class SwarmResult:
    status: SwarmStatus                 # "COMPLETED" | "FAILED"
    text: str
    node_history: list[str]
    results: dict[str, AgentResult]
    iterations: int
    handoffs: int
    usage: TokenUsage
    shared_context: dict[str, dict[str, Any]]
    error: Optional[str] = None
```

Same `handoff_to_agent` tool contract as TS: each member runs as a fresh
clone with memory/session dropped and state copied (never aliased); an
unknown target returns model-visible feedback; a valid handoff raises
`AgentError(SWARM_HANDOFF)` carrying the signal on `.handoff`. Loop
detection: a window set alone enables it with `min_unique_agents`
defaulting to 2 (0 disables). Python deviation: the final `SwarmResult`
arrives as a `{"type": "result", ...}` event (async generators cannot return
values); `run()` interprets it and raises `SWARM_INCOMPLETE` if absent.
`timeout` per node vs. the whole execution is distinguished on timeouts.

---

## A2A client + server

### Client (`a2a_client.py`)

```python
@dataclass
class AgentClientOptions:
    fetch_fn: Callable[[], httpx.AsyncClient] | None = None
    timeout_ms: float = 300_000.0

class AgentClient:
    def __init__(self, endpoint: str, opts: AgentClientOptions | None = None) -> None: ...
    async def stream(
        self,
        text: str,
        context_id: Optional[str] = None,
        signal: Optional[Any] = None,
        on_context: Optional[Callable[[str], None]] = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

class A2AClient:
    def __init__(self, endpoint: str, opts: AgentClientOptions | None = None) -> None: ...
    async def get_card(self) -> dict[str, Any]: ...
    async def send(self, text: str, context_id: Optional[str] = None) -> dict[str, Any]: ...

def a2a_as_tool(client: A2AClient, name: str = "remote_agent",
               description: Optional[str] = None) -> "Tool"
```

Pinned wire contract (parity with the TS server): `stream` POSTs the JSON-RPC
`message/stream` envelope to `{endpoint}/stream` with `Accept: text/event-stream`;
frames are `{contextId, event}`, `{contextId, done, text}`, `{error}`, and
`[DONE]` terminates (both raw and JSON-stringified `[DONE]` accepted).
`AgentClientOptions.timeout_ms` governs every exchange (httpx's 5s default
never applies). 429/5xx/network errors retry with `Retry-After` honored;
server errors map to `A2A_ERROR`, transport to `A2A_TRANSPORT`.

### Server (`a2a_server.py`)

```python
class A2AServer:
    def __init__(
        self,
        card: dict[str, Any],
        agent_factory: AgentFactory,          # (context_id) -> Agent (awaitable ok)
        max_contexts: int = 1000,
        checkpointer: Optional[Checkpointer] = None,
        cors_origins: Optional[list[str]] = None,
        max_body_bytes: int = 10 * 1024 * 1024,
    ) -> None: ...
    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int: ...
    async def stop(self) -> None: ...
```

Same routes as the TS server (card, `/health`, `message/send`, SSE `/stream`)
on a plain asyncio TCP server. Fresh agent per contextId (insertion-ordered
dict = LRU); durable HITL threads via the checkpointer (`threadId=contextId`);
oversized bodies raise `A2A_BODY_TOO_LARGE`.

---

## Serving framework.json (`serve.py`)

```python
class AgentDef(BaseModel):       # framework.json agent entry (camelCase aliases)
    name: str
    description: str = ""
    system_prompt: str
    model: str
    port: int | None = None
    skills: list[str] = []
    filesystem: str | None = None
    todos: bool = False
    max_steps: int = 8
    session_dir: str | None = None
    env_file: str | None = None

class ServerConfig(BaseModel):   # host (default 127.0.0.1), base_port (default 8484)
class HttpConfig(BaseModel):     # cors_origins
class CheckpointerConfig(BaseModel):  # dir
class FrameworkFile(BaseModel):  # server, env_file, env, checkpointer, http, agents

@dataclass
class LoadedFrameworkFile:
    server: ServerConfig
    env_file: Union[str, list[str]] | None
    env: dict[str, str] | None
    checkpointer: CheckpointerConfig | None
    http: HttpConfig
    agents: list[AgentDef]

def load_env_file(path: str) -> None            # setdefault — process env wins
def load_framework_config(path: str) -> LoadedFrameworkFile

@dataclass
class BuiltAgent: agent: Agent; card: dict[str, Any]

async def build_served_agent(defn: AgentDef, session_id: str | None = None) -> BuiltAgent

@dataclass
class ServedAgent: name: str; url: str; card_url: str; server: A2AServer

@dataclass
class FrameworkApp:
    agents: list[ServedAgent]
    async def stop(self) -> None: ...

async def serve_framework(file: Union[LoadedFrameworkFile, str, Path]) -> FrameworkApp

def main(argv: list[str] | None = None) -> int   # python -m framework_agents.serve CLI
```

Skill discovery appends the catalog to the system prompt; middleware
assembly (filesystem + todos when available); the session manager is wired
per context (`{name}:{contextId}` when `session_dir` is set). Python
deviation: serving is lazy — only the card is built at boot and
`init_chat_model` runs per context.

---

## Semantic graph (`semantic.py`)

```python
@dataclass
class Quad:
    subject: str
    predicate: str
    object: str
    graph: str | None = None
    source: str | None = None       # provenance — required on explicit writes
    derived_from: str | None = None
    rule: str | None = None

@dataclass
class Violation: message: str; node: str | None = None; path: str | None = None
@dataclass
class ShapeReport: valid: bool; violations: list[Violation] = field(default_factory=list)
@dataclass
class Derivation: premises: list[Quad]; rule: str

class GraphStore(Protocol):
    async def add(self, quads: list[Quad]) -> None: ...
    async def match(s?, p?, o?, g?) -> list[Quad]: ...
    async def validate(self, quads: list[Quad]) -> ShapeReport: ...
    async def why(self, quad: Quad) -> Derivation | None: ...

@dataclass
class PropertyShape: path: str; min_count: int | None = None; max_count: int | None = None
@dataclass
class NodeShape: target_class: str; properties: list[PropertyShape]
@dataclass
class TriplePattern: subject: str; predicate: str; object: str
@dataclass
class GraphRule: id: str; version: str | None = None; where: list[TriplePattern]; construct: list[TriplePattern]

class InMemoryGraphStore:
    def __init__(self, opts: StoreOptions | None = None) -> None: ...
    def add_shapes(self, *shapes: NodeShape) -> "InMemoryGraphStore": ...
    def add_rules(self, *rules: GraphRule) -> "InMemoryGraphStore": ...
    def ontology_shapes(self) -> list[NodeShape]: ...
    def ontology_rules(self) -> list[GraphRule]: ...
    async def add(self, quads: list[Quad]) -> None: ...
    async def match(self, s=None, p=None, o=None, g=None) -> list[Quad]: ...
    async def validate(self, quads: list[Quad]) -> ShapeReport: ...
    async def why(self, quad: Quad) -> Derivation | None: ...
    def check_shapes(self, quads: list[Quad]) -> ShapeReport: ...
```

The entailment closure implements the same rule set as TS: `RDFS-SUB-TRANS`,
`RDFS-TYPE`, `RDFS-SUBPROP-TRANS`, `RDFS-SUBPROP`, `RDFS-DOMAIN`,
`RDFS-RANGE`, `OWL-TRANSITIVE`, `OWL-SYMMETRIC`, `OWL-INVERSE`, plus
version-tagged `GraphRule`s (`id@version`).

### SPARQL subset + tools

```python
def is_literal(term: str) -> bool
def term_datatype(term: str) -> str | None

@dataclass
class PatternBinding: vars: dict[str, str]; quads: list[Quad]
def match_patterns(patterns: list[TriplePattern], quads: list[Quad]) -> list[PatternBinding]
def instantiate(term: str, vars: dict[str, str]) -> str

@dataclass
class SparqlSelect: variables: list[str]; patterns: list[TriplePattern]
def parse_select(query: str) -> SparqlSelect
async def execute_select(store: GraphStore, query: str) -> list[dict[str, str]]

def sparql_select_tool(store: GraphStore) -> Tool      # "sparql_select"
def ontology_lookup_tool(store: GraphStore) -> Tool     # "ontology_lookup"
def graph_add_tool(store: GraphStore) -> Tool           # "graph_add"
def claim_check_tool(store: GraphStore) -> Tool        # "claim_check"
```

`parse_select` accepts `SELECT ?vars WHERE { s p o . ... }` (∆vars, `<iris>`,
prefixed names, literals) and throws on anything else — a documented subset,
not full SPARQL. Tool meanings are the TS ones (validated autonomous writes,
claim verdicts `grounded | unknown | contradicted`).

### GraphMemory

```python
class GraphMemory:
    def __init__(self, store: GraphStore, inner: Memory, facts: int = 8) -> None: ...
    async def load(self) -> list[ChatMessage]: ...   # + top-k relevant closure facts
    async def save(self, messages: list[ChatMessage]) -> None: ...
    async def clear(self) -> None: ...
    async def export_state(self) -> dict[str, list[Quad]]: ...
    async def import_state(self, state) -> None: ...
```

### Entity linking + JSON-LD

```python
@dataclass
class LinkedEntity:
    iri: str
    label: str | None = None
    score: float = 0
    same_as_group: list[str] = field(default_factory=list)

class EntityLinker:
    def __init__(self, store: GraphStore, label_predicates: Iterable[str] | None = None) -> None: ...
    async def link(self, mention: str, k: int = 5) -> list[LinkedEntity]: ...

def link_entity_tool(linker: EntityLinker) -> Tool       # "link_entity"

@dataclass
class MappingProposals:
    store: "GraphStore | None" = None
    autonomy: bool = True
    async def propose(self, from_: str, to: str, source: str) -> MappingProposal: ...
    async def decide(self, store: GraphStore, id: str, decision: MappingDecision) -> MappingProposal | None: ...

def propose_mapping_tool(proposals: MappingProposals) -> Tool  # "propose_mapping"

def quads_to_json_ld(quads: list[Quad], prefixes: Mapping[str, str] | None = None) -> dict[str, Any]
def json_ld_to_quads(doc: Mapping[str, Any], prefixes: Mapping[str, str] | None = None) -> list[Quad]
```

Named graphs round-trip through JSON-LD; provenance does not. With autonomy
(default) + a bound store, `propose_mapping` commits immediately as a
provenance-carrying `skos:altLabel`; `autonomy=False` queues for `decide()`.

### Ontology review + migration (`ontology_review.py` + `semantic.py`)

```python
@dataclass
class TermStat:
    term: str
    kind: str
    uses: int = 0
    axiom_count: int = 0
    label: str | None = None

@dataclass
class OntologyReview:
    classes: list[TermStat]
    properties: list[TermStat]
    undeclared: list[TermStat]
    unused: list[str]
    unlabeled: list[str]
    missing_domain: list[str]
    missing_range: list[str]
    explicit_quads: int = 0
    closure_quads: int = 0

async def review_ontology(store: InMemoryGraphStore) -> OntologyReview

async def propose_shapes(
    store: InMemoryGraphStore,
    min_instances: int = 2,     # classes with fewer instances produce no shape
    coverage: float = 0.5,
) -> list[NodeShape]

@dataclass
class RejectedShape: shape: NodeShape; violations: list[str]
@dataclass
class ShapeAdoption: adopted: list[NodeShape]; rejected: list[RejectedShape]

async def adopt_shapes(store: InMemoryGraphStore, shapes: list[NodeShape]) -> ShapeAdoption

def review_ontology_tool(store: InMemoryGraphStore) -> Tool       # "review_ontology"
def propose_shapes_tool(store: InMemoryGraphStore, ...) -> Tool   # "propose_shapes"

async def run_conformance(store, battery: list[ConformanceCheck]) -> ConformanceReport
async def migrate_t_box(old_store, spec, ...) -> MigrationResult  # TS migrateTBox
```

The migration report/pre-battery/post-battery semantics match TS
`migrateTBox`; the Python docstring notes the default TBox named-graph URN is
`urn:framework:ontology:<version>` (configurable).

---

## Remote SPARQL (`remote_sparql.py`)

```python
@dataclass
class SparqlAuth:
    type: Literal["basic", "bearer", "header"]
    user: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    name: Optional[str] = None
    value: Optional[str] = None

@dataclass
class RemoteSparqlAdapter:
    endpoint: str
    update_endpoint: str | None = None
    auth: SparqlAuth | None = None
    prefixes: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    fetch_fn: Callable[[], httpx.AsyncClient] | None = None
    timeout_ms: float = 30_000
    max_retries: int = 2

    def with_prefixes(self, query: str) -> str: ...
    async def select(self, query: str) -> list[dict[str, str]]: ...
    async def update(self, query: str) -> None: ...            # SPARQL 1.1 UPDATE (opt-in)

def remote_sparql_tool(adapter: RemoteSparqlAdapter, allow_update: bool = False) -> Tool
```

Retries for 429/5xx/network honor `Retry-After`; a descriptive `User-Agent` is
sent by default (public endpoints 403 without one). The tool is
`remote_sparql_select` (read-only) or `remote_sparql` (SELECT + opt-in
UPDATE).

---

## MCP client (`mcp_client.py`)

```python
class McpToolDef(dict):            # {name, description?, inputSchema?}

class McpStdioClient:
    def __init__(self, command: str, args: list[str] | None = None,
                 server_id: str | None = None) -> None: ...
    async def connect(self) -> None: ...
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def call_tool(self, name: str, args: Any) -> str: ...
    async def close(self) -> None: ...

class McpHttpClient:
    def __init__(self, url: str, headers: Optional[dict[str, str]] = None, *,
                 timeout_ms: Optional[float] = None,
                 http_client: Optional[httpx.AsyncClient] = None,
                 client_name: str = CLIENT_NAME,
                 server_id: Optional[str] = None) -> None: ...
    async def connect(self) -> None: ...
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def call_tool(self, name: str, args: Any) -> str: ...
    async def close(self) -> None: ...

class McpClient(Protocol): ...     # server_id / list_tools / call_tool / close

def json_schema_to_model(schema: dict[str, Any], name: str = "McpArgs") -> Type[BaseModel]

async def mcp_tools_to_framework(
    client: McpClient,
    defs: Optional[list[dict[str, Any]]] = None,
) -> list[Tool]

async def connect_mcp_http(url: str, headers: Optional[dict[str, str]] = None, **opts) -> McpHttpClient
async def connect_mcp_stdio(command: str, *args) -> McpStdioClient
```

JSON Schema input schemas convert to a generated
pydantic model (`PermissiveArgs` fallback — one unbridgeable schema degrades
to a permissive tool, never a crash). Tool payloads include only fields the
model actually set (no planted `null`s). `\n`-delimited JSON-RPC on stdio;
Streamable HTTP on POST with JSON or SSE responses. Error codes match TS:
`MCP_ERROR`, `MCP_TRANSPORT` (network/5xx retryable), `MCP_TOOL_ERROR`,
`MCP_NOT_CONNECTED`.

---

## Skills (`skills.py`) & prompts (`prompts.py`)

```python
@dataclass
class Skill:
    name: str
    description: str
    version: str
    path: str
    body: str
    tools: list[Tool] = field(default_factory=list)

def parse_skill_file(content: str, path: str = "<inline>") -> tuple[dict[str, str], str]

class SkillRegistry:
    def register(self, skill: Skill) -> "SkillRegistry": ...
    def get(self, name: str) -> Skill | None: ...
    def list(self) -> list[Skill]: ...
    def as_tools(self) -> list[Tool]: ...

def discover_skills(dirs: Iterable[str], strict: bool = False) -> list[Skill]

def tokenize(text: str) -> list[str]
@dataclass
class ScoredSkill: skill: Skill; score: float
def score_skills(registry: SkillRegistry, input_text: str) -> list[ScoredSkill]
def match_skills(registry: SkillRegistry, input_text: str, threshold: float = 0.05) -> list[Skill]

@dataclass
class PromptTemplate:
    name: str
    template: str
    version: str = "0.1.0"
    def render(self, vars: Optional[dict[str, Any]] = None) -> str: ...

def define_prompt(name: str, template: str, version: str = "0.1.0") -> PromptTemplate
def load_prompt_file(path: str | Path) -> PromptTemplate
def ab_prompts(variants: list[Any], pick: Optional[str] = None,
               weights: Optional[list[float]] = None) -> PromptTemplate
```

SKILL.md frontmatter + markdown body, BOM/CRLF tolerant; `$name` mentions
always win (word-boundary matched); TF-IDF cosine routing is
dependency-free. `define_prompt` is the TS-parity helper even though it is not
in `__all__` (reach it via `framework_agents.prompts.define_prompt`).

---

## Sandbox (`sandbox.py`)

```python
def run_javascript_tool(timeout_ms: float = 2000, max_output_chars: int = 4000) -> Tool

class SandboxBackend:
    def __init__(self, root: str, timeout_ms: float = 30000,
                 max_buffer: int = 4 * 1024 * 1024, max_output_chars: int = 12000,
                 env: Optional[dict[str, str]] = None) -> None: ...
    def tools(self) -> list[Tool]: ...   # [execute]; pair with filesystem_tools for files

def create_sandbox_backend(
    root: str,
    timeout_ms: float = 30000,
    max_buffer: int = 4 * 1024 * 1024,
    max_output_chars: int = 12000,
    env: Optional[dict[str, str]] = None,
) -> SandboxBackend
```

`run_javascript` runs a safe-subset interpreter in a daemon thread (no
network/fs; the completion value becomes the result; timeouts raise
retryable `SANDBOX_TIMEOUT`). `execute` runs a shell command in the rooted
workspace with a scrubbed env — output above the buffer fails
`SANDBOX_OUTPUT_TOO_LARGE`, timeouts raise retryable `SANDBOX_TIMEOUT`,
oversized-but-under-buffer output spills to `.spill-N.txt`. Documented
deviation vs TS: the exec thread cannot be hard-killed mid-statement (the
child is killed on timeout; the thread dies at process exit), and the
backend contributes only `execute` (combine with `filesystem_tools(root)`
for file tools).

---

## RAG (`rag.py`)

```python
@dataclass
class ScoredDoc:
    id: str
    text: str
    score: float
    meta: Optional[dict[str, Any]] = None

@runtime_checkable
class Retriever(Protocol):
    id: str
    def search(self, query: str, k: int = 4) -> Awaitable[list[ScoredDoc]]: ...

class InMemoryRetriever:          # id "tfidf"
    def __init__(self, docs: list[Any]) -> None: ...
    async def search(self, query: str, k: int = 4) -> list[ScoredDoc]: ...

def retrieval_tool(retriever: Retriever, name: str = "retrieve", k: int = 4) -> Tool
```

Dependency-free TF-IDF cosine retriever over static documents (idf weights
both norms, preserving the TS ranking). `retrieval_tool` (named `retrieve` by
default, k baked in as the arg default) returns top passages with scores.

---

## Evals (`evals.py`)

```python
@dataclass
class EvalExpect:
    contains: list[str] = field(default_factory=list)
    tool_used: list[str] = field(default_factory=list)
    no_structured_error: bool | None = None

@dataclass
class EvalCase:
    id: str
    input: str
    expect: EvalExpect | None = None

@dataclass
class EvalScore:
    id: str
    passed: bool
    detail: str
    usage: TokenUsage

@dataclass
class EvalRun:
    scores: list[EvalScore]
    pass_rate: float

def load_jsonl(path: str | Path) -> list[EvalCase]
async def run_evals(cases: list[EvalCase], make_agent: Callable[[EvalCase], Agent]) -> EvalRun
def print_report(scores: list[EvalScore], pass_rate: float) -> None
```

---

## Naming differences Python <-> TypeScript

Both barrels are name-for-name parity except these deliberate renames
(identical semantics, native naming conventions):

| Python | TypeScript |
|---|---|
| `load_prompt_file` | `loadPromptFile` |
| `ab_prompts` | `abPrompts` |
| `run_evals` | `runEval` |
| `run_with_budget`, `with_file_lock` | `runWithBudget`, `withFileLock` |
| `trim_dangling`, `select_snapshot_fields` | `trimDangling`, `selectSnapshotFields` |
| `estimate_tokens` (in `conversation`) | `estimateTokens` (in `utils`) |
| `model_retry` | `modelRetryMiddleware` |
| `tool_retry` | `toolRetryMiddleware` (TS's simple `toolRetry` is folded in) |
| `model_fallback` | `modelFallbackMiddleware` |
| `tool_call_limit` | `toolCallLimitMiddleware` |
| `policy_filter` | `policyFilterMiddleware` |
| `pii_redact` | `piiMiddleware` |
| `context_editing_middleware` | `contextEditingMiddleware` |
| `human_in_the_loop` | `humanInTheLoopMiddleware` |
| `get_retryable` / `stamp_retryable` | `getRetryable` / `stampRetryable` |
| `a2a_as_tool` | `a2aAsTool` |
| `mcp_tools_to_framework` | `mcpToolsToFramework` |
| `take/load_snapshot`, `session/save_snapshot` | `takeSnapshot`/`loadSnapshot`, `saveSnapshot` |
| `hitl_key` | `hitlKey` |
| `score_skills` / `match_skills` | `scoreSkills` / `matchSkills` |
| `parse_skill_file` | `parseSkillFile` (returns `(meta, body)` vs the flattened object) |
| `run_conformance` | `runConformance` |
| `migrate_t_box` | `migrateTBox` |
| `adopt_shapes` / `propose_shapes` | `adoptShapes` / `proposeShapes` |
| `review_ontology` (+ `_tool`) | `reviewOntology` (+ `reviewOntologyTool`) |
| `remote_sparql_tool` | `remoteSparqlTool` |
| `retrieval_tool` | `retrievalTool` |
| `console_events` | `consoleEvents` |
| `tracing_events` | `tracingEvents` |
| `provider_strategy` / `tool_strategy` | `providerStrategy` / `toolStrategy` |
| `parameters_schema` | — (TS derives the JSON Schema from zod internally) |
| `json_schema_to_model` | `jsonSchemaToZod` |
| pydantic models use snake_case fields with camelCase wire aliases (`TokenUsage.input_tokens` etc.) | native camelCase fields |
| Python-only: `Middleware` class, `run_before_model`/`compose_*`/`run_after_*` chain helpers, `ScriptedModelProvider`, `AbortHandle`, `PendingCall`, `ConsoleEvents`/`TracingEvents` classes, `Budget` kept | TS-only: `Brand`/`AgentId`/`RunId` types, `backoffDelayMs`, `matchGlob`, `filesystemTools` glob-free sibling `sandbox` helpers, `providerFromEnv`, `ModelCapability` type, `__resetConfig` |

Note on the retry verdict mechanism: TS stamps the verdict onto an error via a
well-known global symbol; Python stores it as the `framework_retryable`
attribute. The observable `stamp_retryable` / `get_retryable` contract is
identical in both.

---

## Related pages

- [TypeScript API reference](typescript.md) — the canonical surface this port mirrors.
- [Cross-language parity](../reference/parity.md) — port matrix, conformance story, deviations.
- [Errors & retry contract](errors.md) — code table shared with TypeScript.