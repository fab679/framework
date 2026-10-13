import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { z } from "zod";
import { AgentError } from "../types/index.js";
import { Agent } from "../agent/index.js";
import type { Checkpointer } from "../checkpoint/index.js";
import { defineTool, type Tool } from "../tools/index.js";

/**
 * Agent-to-Agent (A2A) interop — The Framework agents calling agents on
 * other platforms, and vice versa. Minimal core of the open protocol:
 * agent-card discovery + JSON-RPC message/send over HTTP, with per-context
 * isolation on the server side.
 */

export interface AgentCard {
  name: string;
  description?: string;
  version?: string;
  skills?: { name: string; description?: string }[];
}

export interface A2AClientOptions {
  fetchFn?: typeof fetch;
  timeoutMs?: number;
}

function textOfParts(parts: unknown): string {
  if (!Array.isArray(parts)) return "";
  return parts
    .map((p) => {
      if (typeof p === "string") return p;
      if (p && typeof p === "object") {
        const o = p as Record<string, unknown>;
        if (typeof o.text === "string") return o.text;
      }
      return "";
    })
    .join("");
}

function parseSendResult(result: unknown): string {
  if (typeof result === "string") return result;
  if (result && typeof result === "object") {
    const o = result as Record<string, unknown>;
    const msg = o.message as { parts?: unknown } | undefined;
    if (msg?.parts) return textOfParts(msg.parts);
    if (typeof o.text === "string") return o.text;
    return JSON.stringify(result);
  }
  return String(result ?? "");
}

/** Consume a remote A2A agent like a local one. */
export class A2AClient {
  private fetchFn: typeof fetch;
  private timeoutMs: number;

  constructor(
    readonly endpoint: string,
    opts: A2AClientOptions = {},
  ) {
    this.endpoint = endpoint.replace(/\/$/, "");
    this.fetchFn = opts.fetchFn ?? fetch;
    this.timeoutMs = opts.timeoutMs ?? 300000;
  }

  async getCard(): Promise<AgentCard> {
    let res: Response;
    try {
      res = await this.fetchFn(`${this.endpoint}/.well-known/agent-card.json`, {
        headers: { accept: "application/json" },
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (err) {
      throw new AgentError("A2A_TRANSPORT", `Agent card fetch failed: ${err instanceof Error ? err.message : String(err)}`, true);
    }
    if (!res.ok) throw new AgentError("A2A_CARD", `Agent card fetch failed: HTTP ${res.status}`, res.status >= 500);
    const card = (await res.json()) as AgentCard;
    if (!card?.name) throw new AgentError("A2A_CARD", "Invalid agent card (missing name)", false);
    return card;
  }

  async send(text: string, contextId?: string): Promise<{ text: string; contextId?: string }> {
    let res: Response;
    try {
      res = await this.fetchFn(this.endpoint, {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: `a2a-${Date.now().toString(36)}`,
          method: "message/send",
          params: {
            message: { role: "user", parts: [{ kind: "text", text }] },
            ...(contextId ? { contextId } : {}),
          },
        }),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (err) {
      throw new AgentError("A2A_TRANSPORT", `A2A send failed: ${err instanceof Error ? err.message : String(err)}`, true);
    }
    if (!res.ok) throw new AgentError("A2A_TRANSPORT", `A2A HTTP ${res.status}`, res.status >= 500);
    const rpc = (await res.json()) as { result?: unknown; error?: { message: string; code: number } };
    if (rpc.error) throw new AgentError("A2A_ERROR", `Remote agent error: ${rpc.error.message}`, false);
    // Servers return the (possibly newly minted) contextId inside `result`.
    const resultContextId =
      rpc.result && typeof rpc.result === "object" ? (rpc.result as { contextId?: string }).contextId : undefined;
    return { text: parseSendResult(rpc.result), contextId: resultContextId ?? contextId };
  }
}

/** Wrap a remote agent as a framework Tool for orchestrators. */
export function a2aAsTool(client: A2AClient, opts: { name?: string; description?: string } = {}): Tool {
  return defineTool({
    name: opts.name ?? "remote_agent",
    description: opts.description ?? `Remote A2A agent at ${client.endpoint}`,
    schema: z.object({ input: z.string(), contextId: z.string().optional() }),
    execute: async ({ input, contextId }: { input: string; contextId?: string }) =>
      (await client.send(input, contextId)).text,
  });
}

export interface A2AServerOptions {
  card: AgentCard;
  /**
   * Fresh agent per contextId (isolation). Retained up to maxContexts (LRU).
   * Return the SAME agent for a contextId to keep state in memory across
   * turns of one conversation, but never share one instance across contexts —
   * shared history leaks conversations between users.
   */
  agentFactory: (contextId: string) => Agent | Promise<Agent>;
  maxContexts?: number;
  port?: number;
  host?: string;
  /** Durable HITL threads: runs execute with { checkpointer, threadId: contextId }. */
  checkpointer?: Checkpointer;
  /** Browser frontends: reflected as Access-Control-Allow-Origin (+ OPTIONS handling). */
  corsOrigins?: string[];
  /** Max JSON-RPC request body bytes. Default 10 MiB. */
  maxBodyBytes?: number;
}

/** Serve a Framework agent over A2A (card + message/send). */
export class A2AServer {
  private server?: Server;
  private contexts = new Map<string, Agent>();
  private maxContexts: number;

  constructor(private opts: A2AServerOptions) {
    this.maxContexts = Math.max(1, opts.maxContexts ?? 1000);
  }

  private corsHeaders(origin: string | undefined): Record<string, string> {
    const allowed = this.opts.corsOrigins ?? [];
    if (!allowed.length || !origin) return {};
    if (!allowed.includes("*") && !allowed.includes(origin)) return {};
    return {
      "access-control-allow-origin": allowed.includes("*") ? "*" : origin,
      "access-control-allow-methods": "GET, POST, OPTIONS",
      "access-control-allow-headers": "content-type, authorization",
    };
  }

  private runOpts(contextId: string): { checkpointer?: Checkpointer; threadId?: string } {
    return this.opts.checkpointer ? { checkpointer: this.opts.checkpointer, threadId: contextId } : {};
  }

  private async agentFor(contextId: string): Promise<Agent> {
    const existing = this.contexts.get(contextId);
    if (existing) {
      this.contexts.delete(contextId);
      this.contexts.set(contextId, existing);
      return existing;
    }
    const agent = await this.opts.agentFactory(contextId);
    this.contexts.set(contextId, agent);
    while (this.contexts.size > this.maxContexts) {
      const oldest = this.contexts.keys().next().value;
      if (oldest === undefined) break;
      this.contexts.delete(oldest);
    }
    return agent;
  }

  /** Read a request body with a hard cap; JSON.parse errors to the caller. */
  private readBody(req: IncomingMessage): Promise<string> {
    const max = this.opts.maxBodyBytes ?? 10 * 1024 * 1024;
    return new Promise<string>((resolve, reject) => {
      let data = "";
      req.on("data", (c) => {
        data += c;
        if (data.length > max) {
          req.destroy();
          reject(new AgentError("A2A_BODY_TOO_LARGE", `Request body exceeds ${max} bytes`, false));
        }
      });
      req.on("end", () => resolve(data));
      req.on("error", reject);
    });
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const cors = this.corsHeaders(req.headers.origin);
    const send = (code: number, body: unknown): void => {
      res.writeHead(code, { "content-type": "application/json", ...cors });
      res.end(JSON.stringify(body));
    };
    try {
      if (req.method === "OPTIONS") {
        res.writeHead(204, { ...cors });
        res.end();
        return;
      }
      if (req.method === "GET" && (req.url === "/ok" || req.url === "/health")) {
        send(200, { ok: true });
        return;
      }
      if (req.method === "GET" && req.url === "/.well-known/agent-card.json") {
        send(200, this.opts.card);
        return;
      }
      if (req.method === "POST" && (req.url === "/" || req.url === "")) {
        const rpc = JSON.parse(await this.readBody(req)) as {
          id?: number | string;
          method?: string;
          params?: { message?: { parts?: unknown }; contextId?: string };
        };
        if (rpc.method !== "message/send") {
          send(200, { jsonrpc: "2.0", id: rpc.id, error: { code: -32601, message: `Unknown method: ${rpc.method}` } });
          return;
        }
        const contextId = rpc.params?.contextId ?? `ctx-${Date.now().toString(36)}`;
        const text = textOfParts(rpc.params?.message?.parts);
        const result = await (await this.agentFor(contextId)).run(text, this.runOpts(contextId));
        send(200, {
          jsonrpc: "2.0",
          id: rpc.id,
          result: { message: { role: "agent", parts: [{ kind: "text", text: result.text }] }, contextId },
        });
        return;
      }
      if (req.method === "POST" && req.url === "/stream") {
        const rpc = JSON.parse(await this.readBody(req)) as {
          id?: number | string;
          params?: { message?: { parts?: unknown }; contextId?: string };
        };
        const contextId = rpc.params?.contextId ?? `ctx-${Date.now().toString(36)}`;
        const text = textOfParts(rpc.params?.message?.parts);
        res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
        const write = (payload: unknown): void => {
          res.write(`data: ${JSON.stringify(payload)}\n\n`);
        };
        // Stop the agent when the client disconnects (headers already sent, so
        // we can only report the abort on the wire as a final error frame).
        const disconnect = new AbortController();
        res.on("close", () => {
          if (!res.writableEnded) disconnect.abort();
        });
        try {
          const gen = (await this.agentFor(contextId)).stream(text, {
            signal: disconnect.signal,
            ...this.runOpts(contextId),
          });
          for (;;) {
            const next = await gen.next();
            if (next.done) {
              write({ contextId, done: true, text: next.value.text });
              break;
            }
            write({ contextId, event: next.value });
          }
        } catch (err) {
          write({ contextId, error: err instanceof Error ? err.message : String(err) });
        }
        write("[DONE]");
        res.end();
        return;
      }
      send(404, { error: "not found" });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (res.headersSent) {
        // Mid-stream failure (e.g. oversized body): SSE headers already out.
        try {
          res.write(`data: ${JSON.stringify({ error: message })}\n\n`);
          res.write("data: [DONE]\n\n");
          res.end();
        } catch {
          // Socket gone — nothing more to do.
        }
        return;
      }
      send(
        err instanceof AgentError && err.code === "A2A_BODY_TOO_LARGE" ? 413 : 200,
        { jsonrpc: "2.0", id: null, error: { code: -32603, message } },
      );
    }
  }

  async start(port?: number, host?: string): Promise<number> {
    const listenPort = port ?? this.opts.port ?? 0;
    const listenHost = host ?? this.opts.host ?? "127.0.0.1";
    this.server = createServer((req, res) => void this.handle(req, res));
    await new Promise<void>((resolve) => this.server?.listen(listenPort, listenHost, () => resolve()));
    return (this.server.address() as { port: number }).port;
  }

  async stop(): Promise<void> {
    await new Promise<void>((resolve, reject) =>
      this.server ? this.server.close((e) => (e ? reject(e) : resolve())) : resolve(),
    );
    this.server = undefined;
    this.contexts.clear();
  }
}
