import type { AgentResult } from "../types/index.js";

/**
 * Typed run events with independent projections.
 * Consume via `for await (const e of agent.stream(input))`.
 */
export type StreamEvent =
  | { type: "step"; step: number }
  | { type: "token"; delta: string }
  | { type: "thinking"; delta: string }
  | { type: "tool_call"; name: string; args: unknown }
  | { type: "tool_result"; name: string; output: unknown }
  | { type: "done"; text: string };

/** Collect a stream into buckets — handy for tests and simple UIs. */
export async function collectStream(
  stream: AsyncGenerator<StreamEvent, AgentResult>,
): Promise<{ events: StreamEvent[]; result: AgentResult }> {
  const events: StreamEvent[] = [];
  for (;;) {
    const next = await stream.next();
    if (next.done) return { events, result: next.value };
    events.push(next.value);
  }
}
