"""framework_agents — Python parity port of packages/typescript.

Import parity with the TS package (barrel over the whole public surface):

    from framework_agents import Agent, AgentOptions, define_tool, ...
    from framework_agents.semantic import InMemoryGraphStore
    from framework_agents.middleware import human_in_the_loop

See packages/python/README.md for the port status matrix.
"""

from .agent import (
    Agent,
    AgentEvents,
    AgentOptions,
    AgentResult,
    ResponseFormat,
    TokenBudgets,
    provider_strategy,
    tool_strategy,
)
from .a2a_client import A2AClient, AgentClient, AgentClientOptions, a2a_as_tool
from .a2a_server import A2AServer
from .checkpoint import Checkpointer, FileCheckpointer, ThreadCheckpoint, with_file_lock
from .config import init_chat_model, load_config
from .conversation import (
    NullConversationManager,
    SlidingWindowConversationManager,
    SummarizingConversationManager,
    estimate_tokens,
    history_tokens,
    trim_dangling,
)
from .errors import AgentError, get_retryable, stamp_retryable
from .evals import EvalCase, EvalScore, load_jsonl, print_report, run_evals
from .memory import FileMemory, InMemoryMemory
from .memory_compaction import CompactedMemory
from .middleware import (
    ClearToolUsesEdit,
    HitlInterrupt,
    ToolRejection,
    context_editing_middleware,
    create_middleware,
    create_sub_agent_middleware,
    filesystem_middleware,
    filesystem_tools,
    hitl_key,
    human_in_the_loop,
    llm_tool_selector_middleware,
    model_call_limit,
    model_fallback,
    model_retry,
    pii_redact,
    policy_filter,
    summarization_middleware,
    todo_list,
    tool_call_limit,
    tool_emulator_middleware,
    tool_retry,
)
from .models import (
    CompleteOptions,
    MockModelProvider,
    ModelCompletion,
    ModelProvider,
    ResponseFormatWire,
    ScriptedModelProvider,
    ToolCallRequest,
    ToolWire,
)
from .models.capabilities import assert_can_handle, supports_vision
from .models.ollama import OllamaOptions, OllamaProvider, ollama
from .models.anthropic import AnthropicOptions, AnthropicProvider
from .models.openai_compatible import (
    OpenAICompatibleOptions,
    OpenAICompatibleProvider,
    provider_from_env,
)
from .observability import console_events, log_error
from .pricing import ModelPrice, price_for, usage_cost
from .prompts import PromptTemplate, ab_prompts, load_prompt_file
from .rag import InMemoryRetriever, retrieval_tool
from .runtime import AbortHandle, Budget, run_with_budget
from .sandbox import create_sandbox_backend, run_javascript_tool
from .sessions import (
    InMemoryStorage,
    LocalFileStorage,
    SessionManager,
    Snapshot,
    select_snapshot_fields,
)
from .state import AgentState
from .streaming import StreamEvent, collect_stream
from .skills import SkillRegistry, discover_skills, match_skills, parse_skill_file, score_skills
from .tools import (
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    define_tool,
    parameters_schema,
)
from .tracing import (
    ConsoleSpanSink,
    JsonlSpanSink,
    MemorySpanSink,
    Span,
    SpanSink,
    tracing_events,
)
from .types import (
    ChatMessage,
    ExecutedToolCall,
    TokenUsage,
    assistant,
    deserialize_messages,
    has_images,
    parse_content_block,
    serialize_messages,
    system,
    text_of,
    tool_result,
    user,
    wire_text_of,
)
from .workflows import WorkflowNode, WorkflowOptions, run_workflow

# Subpackages exported lazily to keep import cost down? — python imports are
# already module-level and cheap here; semantic/mcp/swarm/orchestration are
# exported as modules so the public surface mirrors the TS barrel without
# starving startup.
from . import (  # noqa: E402
    conversation,
    mcp_client,
    middleware,
    orchestration,
    semantic,
    swarm,
)

from .mcp_client import (
    McpHttpClient,
    McpStdioClient,
    connect_mcp_http,
    connect_mcp_stdio,
    mcp_tools_to_framework,
)
from .orchestration import OrchestrationPlan, OrchestratedResult, run_orchestrated
from .ontology_review import adopt_shapes, propose_shapes, review_ontology
from .semantic import (
    EntityLinker,
    GraphMemory,
    InMemoryGraphStore,
    MappingProposals,
    NodeShape,
    Quad,
    execute_select,
    json_ld_to_quads,
    migrate_t_box,
    quads_to_json_ld,
    run_conformance,
)
from .remote_sparql import RemoteSparqlAdapter, remote_sparql_tool
from .serve import (
    FrameworkFile,
    LoadedFrameworkFile,
    build_served_agent,
    load_framework_config,
    serve_framework,
)
from .swarm import Swarm, SwarmResult
from .ontology_review import propose_shapes_tool, review_ontology_tool  # noqa: E402

__all__ = [
    "AbortHandle",
    "A2AClient",
    "AgentState",
    "AnthropicOptions",
    "AnthropicProvider",
    "A2AServer",
    "Agent",
    "AgentClient",
    "AgentClientOptions",
    "AgentError",
    "AgentEvents",
    "AgentOptions",
    "AgentResult",
    "AgentState",
    "Budget",
    "ClearToolUsesEdit",
    "FrameworkFile",
    "LoadedFrameworkFile",
    "ChatMessage",
    "Checkpointer",
    "CompactedMemory",
    "ConsoleSpanSink",
    "EntityLinker",
    "EvalCase",
    "EvalScore",
    "ExecutedToolCall",
    "FileCheckpointer",
    "FileMemory",
    "GraphMemory",
    "HitlInterrupt",
    "InMemoryGraphStore",
    "InMemoryRetriever",
    "InMemoryStorage",
    "InMemoryMemory",
    "JsonlSpanSink",
    "LocalFileStorage",
    "MappingProposals",
    "MemorySpanSink",
    "McpHttpClient",
    "McpStdioClient",
    "MockModelProvider",
    "ModelCompletion",
    "ModelPrice",
    "ModelProvider",
    "NodeShape",
    "NullConversationManager",
    "OllamaOptions",
    "OllamaProvider",
    "OpenAICompatibleOptions",
    "OpenAICompatibleProvider",
    "OrchestrationPlan",
    "OrchestratedResult",
    "PromptTemplate",
    "Quad",
    "ResponseFormat",
    "ResponseFormatWire",
    "ScriptedModelProvider",
    "SessionManager",
    "SlidingWindowConversationManager",
    "Snapshot",
    "Span",
    "SpanSink",
    "StreamEvent",
    "SummarizingConversationManager",
    "Swarm",
    "SwarmResult",
    "ThreadCheckpoint",
    "TokenBudgets",
    "TokenUsage",
    "Tool",
    "ToolCallRequest",
    "ToolContext",
    "ToolRegistry",
    "ToolRejection",
    "ToolResult",
    "ToolWire",
    "WorkflowNode",
    "WorkflowOptions",
    "ab_prompts",
    "a2a_as_tool",
    "assert_can_handle",
    "collect_stream",
    "assistant",
    "connect_mcp_http",
    "context_editing_middleware",
    "create_sub_agent_middleware",
    "connect_mcp_stdio",
    "console_events",
    "adopt_shapes",
    "create_middleware",
    "create_sandbox_backend",
    "define_tool",
    "deserialize_messages",
    "discover_skills",
    "estimate_tokens",
    "execute_select",
    "get_retryable",
    "has_images",
    "filesystem_middleware",
    "filesystem_tools",
    "history_tokens",
    "hitl_key",
    "human_in_the_loop",
    "init_chat_model",
    "json_ld_to_quads",
    "load_framework_config",
    "load_config",
    "load_jsonl",
    "load_prompt_file",
    "log_error",
    "match_skills",
    "mcp_tools_to_framework",
    "migrate_t_box",
    "llm_tool_selector_middleware",
    "model_fallback",
    "model_call_limit",
    "model_retry",
    "ollama",
    "parse_content_block",
    "parse_skill_file",
    "parameters_schema",
    "pii_redact",
    "policy_filter",
    "price_for",
    "provider_from_env",
    "print_report",
    "propose_shapes",
    "propose_shapes_tool",
    "quads_to_json_ld",
    "remote_sparql_tool",
    "RemoteSparqlAdapter",
    "retrieval_tool",
    "review_ontology",
    "review_ontology_tool",
    "run_conformance",
    "run_evals",
    "serve_framework",
    "build_served_agent",
    "run_javascript_tool",
    "run_orchestrated",
    "run_with_budget",
    "run_workflow",
    "summarization_middleware",
    "score_skills",
    "supports_vision",
    "select_snapshot_fields",
    "serialize_messages",
    "stamp_retryable",
    "system",
    "text_of",
    "tool_emulator_middleware",
    "todo_list",
    "tool_call_limit",
    "tool_result",
    "tool_retry",
    "tool_strategy",
    "tracing_events",
    "trim_dangling",
    "usage_cost",
    "user",
    "provider_strategy",
    "wire_text_of",
    "with_file_lock",
    # module handles
    "conversation",
    "mcp_client",
    "middleware",
    "orchestration",
    "semantic",
    "swarm",
]