import type { ChatMessage, AgentResult } from "../types/index.js";

export interface StepEvent {
  step: number;
  messages: ChatMessage[];
}

export interface ToolCallEvent {
  tool: string;
  args: unknown;
}

export interface ToolResultEvent {
  tool: string;
  output: unknown;
}

export interface DoneEvent {
  text: string;
  toolCalls: AgentResult["toolCalls"];
}

/** Observability hooks — plug logger/tracer here, keep core dependency-free. */
export interface AgentEvents {
  onStep?: (e: StepEvent) => void;
  onToolCall?: (e: ToolCallEvent) => void;
  onToolResult?: (e: ToolResultEvent) => void;
  onDone?: (e: DoneEvent) => void;
  /** Streamed text deltas. */
  onToken?: (delta: string) => void;
  /** Streamed thinking/reasoning deltas. */
  onThinking?: (delta: string) => void;
}

/** Minimal ANSI colors, dependency-free. Honors NO_COLOR and non-TTY. */
export function supportsColor(): boolean {
  return !process.env.NO_COLOR && !!process.stdout?.isTTY;
}

const C = {
  reset: "\x1b[0m",
  bold: "\x1b[1m",
  dim: "\x1b[2m",
  cyan: "\x1b[36m",
  yellow: "\x1b[33m",
  green: "\x1b[32m",
  magenta: "\x1b[35m",
  red: "\x1b[31m",
};

function paint(color: keyof typeof C, text: string, on: boolean): string {
  if (!on || color === "reset") return text;
  return `${C[color]}${text}${C.reset}`;
}

export interface ConsoleEventsOptions {
  prefix?: string;
  /** Force colors on/off. Default: auto (TTY without NO_COLOR). */
  colors?: boolean;
}

export function consoleEvents(prefix?: string, opts?: ConsoleEventsOptions): AgentEvents;
export function consoleEvents(opts: ConsoleEventsOptions): AgentEvents;
export function consoleEvents(prefixOrOpts: string | ConsoleEventsOptions = "[agent]", maybeOpts: ConsoleEventsOptions = {}): AgentEvents {
  const { prefix, colors }: { prefix: string; colors: boolean } =
    typeof prefixOrOpts === "string"
      ? { prefix: prefixOrOpts, colors: maybeOpts.colors ?? supportsColor() }
      : { prefix: prefixOrOpts.prefix ?? "[agent]", colors: prefixOrOpts.colors ?? supportsColor() };
  const tag = (color: keyof typeof C, label: string): string => `${paint(color, `${prefix} ${label}`, colors)}`;
  return {
    onStep: (e) => console.log(tag("cyan", `step ${e.step} (${e.messages.length} msgs)`)),
    onToolCall: (e) => console.log(tag("yellow", `tool:call ${e.tool}`), e.args),
    onToolResult: (e) => console.log(tag("green", `tool:result ${e.tool}`), e.output),
    onDone: (e) => console.log(tag("green", paint("bold", "done:", colors)), e.text),
    onToken: (d) => process.stdout.write(d),
    onThinking: (d) => process.stdout.write(paint("dim", d, colors)),
  };
}

/** Red error line for failures (pairs with consoleEvents). */
export function logError(prefix: string, err: unknown, colors?: boolean): void {
  const on = colors ?? supportsColor();
  const message = err instanceof Error ? (err.stack ?? err.message) : String(err);
  console.error(paint("red", `${prefix} error:`, on), message);
}
