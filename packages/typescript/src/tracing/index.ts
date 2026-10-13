import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { AgentError } from "../types/index.js";
import { ToolRejection, type AgentMiddleware } from "../middleware/index.js";
import type { AgentEvents, StepEvent, ToolCallEvent, ToolResultEvent, DoneEvent } from "../observability/index.js";

/**
 * Tracing exporter: OTel-shaped spans (trace/span ids, timestamps,
 * attributes) from AgentEvents, sunk to console or JSONL. No OTel SDK —
 * the shape is compatible, the transport is local-first. Adapters to
 * Langfuse/LangSmith-style HTTP endpoints can POST these spans unchanged.
 */

export interface Span {
  traceId: string;
  spanId: string;
  parentId?: string;
  name: string;
  startedAt: string;
  endedAt?: string;
  attributes: Record<string, unknown>;
}

function hex(n: number): string {
  return [...Array(n)].map(() => Math.floor(Math.random() * 16).toString(16)).join("");
}

export interface SpanSink {
  emit(span: Span): void;
}

export class ConsoleSpanSink implements SpanSink {
  emit(span: Span): void {
    console.log(`[trace] ${span.name}`, JSON.stringify(span.attributes));
  }
}

export class JsonlSpanSink implements SpanSink {
  constructor(private path: string) {
    mkdirSync(dirname(path), { recursive: true });
  }
  emit(span: Span): void {
    appendFileSync(this.path, `${JSON.stringify(span)}\n`, "utf8");
  }
}

export class MemorySpanSink implements SpanSink {
  spans: Span[] = [];
  emit(span: Span): void {
    this.spans.push(span);
  }
}

/** Build AgentEvents that project run/step/tool lifecycle into spans. */
export function tracingEvents(sink: SpanSink, opts: { traceId?: string } = {}): AgentEvents {
  const traceId = opts.traceId ?? hex(32);
  const open = new Map<string, Span>();
  const emit = (name: string, attributes: Record<string, unknown>, parentId?: string): Span => {
    const span: Span = {
      traceId,
      spanId: hex(16),
      ...(parentId ? { parentId } : {}),
      name,
      startedAt: new Date().toISOString(),
      endedAt: new Date().toISOString(),
      attributes,
    };
    sink.emit(span);
    return span;
  };
  return {
    onStep: (e: StepEvent) => {
      emit("agent.step", { step: e.step, messages: e.messages.length });
    },
    onToolCall: (e: ToolCallEvent) => {
      const span = emit("tool.call", { tool: e.tool, args: e.args });
      open.set(e.tool, span);
    },
    onToolResult: (e: ToolResultEvent) => {
      const parent = open.get(e.tool);
      open.delete(e.tool);
      emit("tool.result", { tool: e.tool, output: e.output }, parent?.spanId);
    },
    onToken: (delta: string) => {
      emit("model.token", { delta: delta.slice(0, 200) });
    },
    onThinking: (delta: string) => {
      emit("model.thinking", { delta: delta.slice(0, 200) });
    },
    onDone: (e: DoneEvent) => {
      emit("agent.done", { text: e.text.slice(0, 500), toolCalls: e.toolCalls.length });
    },
  };
}

/**
 * Policy filter: allow/deny tool calls by name pattern before execution.
 * Denied calls become ToolRejection feedback (model continues) or throw
 * POLICY_DENY with exitBehavior "error".
 */
export function policyFilterMiddleware(opts: {
  allow?: (string | RegExp)[];
  deny?: (string | RegExp)[];
  exitBehavior?: "continue" | "error";
}): AgentMiddleware {
  const match = (list: (string | RegExp)[] | undefined, name: string): boolean =>
    // `g`-flagged regexes are stateful across .test() calls — clone without
    // the flag so one tool name can't alternate denied/allowed.
    (list ?? []).some((p) =>
      typeof p === "string" ? p === name : new RegExp(p.source, p.flags.replace("g", "")).test(name),
    );
  return {
    name: "policy-filter",
    beforeTool: (call) => {
      if (opts.deny && match(opts.deny, call.name)) {
        if (opts.exitBehavior === "error") {
          throw new AgentError("POLICY_DENY", `Policy denies tool "${call.name}"`, false);
        }
        throw new ToolRejection(`Policy denies tool "${call.name}". Do not retry it; finish another way.`);
      }
      if (opts.allow && !match(opts.allow, call.name)) {
        throw new ToolRejection(`Policy allows only [${opts.allow.join(", ")}]; "${call.name}" is not allowed.`);
      }
      return undefined;
    },
  };
}
