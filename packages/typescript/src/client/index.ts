import { AgentError } from "../types/index.js";
import type { StreamEvent } from "../streaming/index.js";
import { A2AClient } from "../a2a/index.js";

/**
 * Frontend/server-component client for Framework agents.
 * Framework-agnostic (fetch + async iterators): use directly in TanStack
 * Query loaders, Next.js route handlers/components, Svelte, or plain DOM.
 * React bindings live in framework-agents-react (thin layer over this client).
 */

export interface AgentClientOptions {
  fetchFn?: typeof fetch;
  timeoutMs?: number;
}

export class AgentClient {
  private a2a: A2AClient;
  private fetchFn: typeof fetch;
  readonly endpoint: string;

  constructor(endpoint: string, opts: AgentClientOptions = {}) {
    this.endpoint = endpoint.replace(/\/$/, "");
    this.fetchFn = opts.fetchFn ?? fetch;
    this.a2a = new A2AClient(this.endpoint, opts);
  }

  async card() {
    return this.a2a.getCard();
  }

  async send(text: string, contextId?: string): Promise<{ text: string; contextId?: string }> {
    return this.a2a.send(text, contextId);
  }

  /** Live StreamEvents over SSE (`POST /stream`). Throws AgentError on server errors. */
  async *stream(
    text: string,
    contextId?: string,
    signal?: AbortSignal,
    opts: { onContext?: (contextId: string) => void } = {},
  ): AsyncGenerator<StreamEvent> {
    let res: Response;
    try {
      res = await this.fetchFn(`${this.endpoint}/stream`, {
        method: "POST",
        headers: { "content-type": "application/json", accept: "text/event-stream" },
        // JSON-RPC envelope — the server reads params.message.parts.
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: `a2a-${Date.now().toString(36)}`,
          method: "message/stream",
          params: {
            message: { role: "user", parts: [{ kind: "text", text }] },
            ...(contextId ? { contextId } : {}),
          },
        }),
        ...(signal ? { signal } : {}),
      });
    } catch (err) {
      throw new AgentError("A2A_TRANSPORT", `Stream failed: ${err instanceof Error ? err.message : String(err)}`, true);
    }
    if (!res.ok || !res.body) throw new AgentError("A2A_TRANSPORT", `Stream HTTP ${res.status}`, res.status >= 500);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const flush = function* (chunk: string): Generator<StreamEvent> {
      buffer += chunk;
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        const t = line.trim();
        if (!t.startsWith("data:")) continue;
        const payload = t.slice(5).trim();
        if (payload === "[DONE]") continue;
        let msg: { contextId?: string; event?: StreamEvent; error?: string };
        try {
          msg = JSON.parse(payload) as { contextId?: string; event?: StreamEvent; error?: string };
        } catch {
          continue;
        }
        if (msg.contextId) opts.onContext?.(msg.contextId);
        if (msg.error) throw new AgentError("A2A_ERROR", msg.error, false);
        if (msg.event && typeof msg.event.type === "string") yield msg.event;
      }
    };
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        yield* flush(decoder.decode(value, { stream: true }));
      }
      yield* flush(decoder.decode());
    } finally {
      // Early break/throw/abort: release the connection and stop the
      // upstream agent instead of letting the run finish in the background.
      await reader.cancel().catch(() => {});
    }
  }
}

/**
 * Next.js / TanStack Start route handler bridge: POST { text, contextId? }
 * → SSE stream of agent events. `AgentClient.stream` posts to
 * `${endpoint}/stream`, so mount this at `app/api/agent/stream/route.ts` and
 * point `useAgent({ endpoint: "/api/agent" })` (or AgentClient) at the parent:
 *
 *   import { AgentClient, handleAgentRoute } from "framework-agents";
 *   const client = new AgentClient(process.env.AGENT_URL!);
 *   export const POST = (req: Request) => handleAgentRoute(req, client);
 *
 * The browser disconnect (req.signal) stops the upstream agent run.
 */
export async function handleAgentRoute(req: Request, client: AgentClient): Promise<Response> {
  const { text, contextId } = (await req.json()) as { text: string; contextId?: string };
  let latestContextId = contextId;
  const stream = new ReadableStream({
    async start(controller) {
      const send = (payload: unknown): void => {
        controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(payload)}\n\n`));
      };
      try {
        for await (const event of client.stream(text, contextId, req.signal, {
          onContext: (id) => (latestContextId = id),
        })) {
          send({ contextId: latestContextId, event });
        }
        send({ contextId: latestContextId });
        send("[DONE]");
      } catch (err) {
        send({ error: err instanceof Error ? err.message : String(err) });
      } finally {
        try {
          controller.close();
        } catch {
          // Already closed (e.g. browser disconnect while flushing).
        }
      }
    },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream", "cache-control": "no-cache" } });
}
