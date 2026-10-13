import { useCallback, useEffect, useRef, useState } from "react";
import { AgentClient } from "framework-agents";

/**
 * Streaming chat against a Framework agent server (or Next.js proxy route).
 *
 * ```tsx
 * const { turns, thinking, activity, status, error, contextId, send, cancel, reset } =
 *   useAgent({ endpoint: "/api/agent" });
 * await send("Explain black holes");
 * ```
 *
 * `contextId` mirrors the server-assigned conversation handle — pass it back
 * via `contextId` in options to continue the same conversation, or just reuse
 * this hook instance: `send` keeps the context automatically.
 *
 * Works with TanStack Start the same way — `endpoint` is just a URL that
 * serves AgentClient's `POST {endpoint}/stream` SSE contract (Next.js users
 * can mount `handleAgentRoute` at `/api/agent/stream/route.ts`). For
 * data-fetching integration, drive `send` from a TanStack Query mutation and
 * read `turns`/`status` for UI state.
 */

export interface ChatTurn {
  role: "user" | "assistant";
  text: string;
}

export type AgentStatus = "idle" | "streaming" | "error";

export interface UseAgentOptions {
  /** Agent server URL or proxy route parent (POSTs go to `${endpoint}/stream`). */
  endpoint: string;
  /** Persisted conversation handle; omit to let the server assign one. */
  contextId?: string;
  fetchFn?: typeof fetch;
}

/** What a completed send resolves with — toolCalls are names in call order. */
export interface SendResult {
  text: string;
  toolCalls: string[];
  /** The conversation handle this send used (server-assigned or your prop). */
  contextId?: string;
}

export function useAgent({ endpoint, contextId, fetchFn }: UseAgentOptions) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [thinking, setThinking] = useState("");
  const [activity, setActivity] = useState<string[]>([]);
  const [status, setStatus] = useState<AgentStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [assignedContextId, setAssignedContextId] = useState<string | undefined>(contextId);
  const abortRef = useRef<AbortController | null>(null);
  const contextRef = useRef<string | undefined>(contextId);

  // Stop the stream when the component unmounts — otherwise the SSE
  // connection (and the server-side agent run) keeps going by itself.
  useEffect(() => () => abortRef.current?.abort(), []);

  const send = useCallback(
    async (text: string): Promise<SendResult> => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setError(null);
      setStatus("streaming");
      setThinking("");
      setTurns((prev) => [...prev, { role: "user", text }, { role: "assistant", text: "" }]);
      const toolCalls: string[] = [];
      let finalText = "";
      // Aborted/erroring sends must not leave an empty assistant bubble.
      const dropTrailingEmptyTurn = (): void => {
        setTurns((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant" && last.text === "") return prev.slice(0, -1);
          return prev;
        });
      };
      const client = new AgentClient(endpoint, ...(fetchFn ? [{ fetchFn }] : []));
      try {
        for await (const event of client.stream(text, contextRef.current, controller.signal, {
          // Keep the server-assigned handle so the next send continues this
          // conversation instead of starting a fresh context.
          onContext: (id) => {
            contextRef.current = id;
            setAssignedContextId(id);
          },
        })) {
          if (controller.signal.aborted) {
            dropTrailingEmptyTurn();
            break;
          }
          if (event.type === "token") {
            const delta = event.delta;
            setTurns((prev) => {
              const next = [...prev];
              next[next.length - 1] = { role: "assistant", text: next[next.length - 1].text + delta };
              return next;
            });
          } else if (event.type === "thinking") {
            const delta = event.delta;
            setThinking((prev) => prev + delta);
          } else if (event.type === "done" && event.text) {
            // Providers/middleware that don't stream deltas report the full
            // answer only here — reconcile instead of leaving an empty turn.
            finalText = event.text;
            const final = event.text;
            setTurns((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant") next[next.length - 1] = { role: "assistant", text: final };
              return next;
            });
          } else if (event.type === "tool_call") {
            toolCalls.push(event.name);
            setActivity((prev) => [...prev, `calling ${event.name}…`]);
          } else if (event.type === "tool_result") {
            setActivity((prev) => [...prev, `${event.name} done`]);
          }
        }
        // A newer send() took over (or this one was aborted): let it own status.
        if (abortRef.current === controller && !controller.signal.aborted) setStatus("idle");
        return { text: finalText, toolCalls, contextId: contextRef.current };
      } catch (err) {
        if (controller.signal.aborted || abortRef.current !== controller) {
          return { text: finalText, toolCalls, contextId: contextRef.current };
        }
        dropTrailingEmptyTurn();
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
        return { text: finalText, toolCalls, contextId: contextRef.current };
      }
    },
    [endpoint, fetchFn],
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    setStatus("idle");
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setTurns([]);
    setThinking("");
    setActivity([]);
    setError(null);
    setStatus("idle");
    contextRef.current = undefined;
    setAssignedContextId(undefined);
  }, []);

  return { turns, thinking, activity, status, error, contextId: assignedContextId, send, cancel, reset };
}