import { spawn, type ChildProcess } from "node:child_process";
import { z } from "zod";
import { AgentError } from "../types/index.js";
import { defineTool, type Tool } from "../tools/index.js";

/**
 * MCP (Model Context Protocol) client.
 * - Streamable HTTP transport (POST JSON-RPC; JSON or SSE responses).
 * - stdio transport (spawned server, line-delimited JSON-RPC).
 * - `mcpToolsToFramework` bridges MCP tool defs to framework Tools,
 *   converting JSON Schema input schemas to zod.
 */

export interface McpToolDef {
  name: string;
  description?: string;
  inputSchema: Record<string, unknown>;
}

export interface McpClientOptions {
  fetchFn?: typeof fetch;
  headers?: Record<string, string>;
  clientName?: string;
}

interface JsonRpcResponse {
  jsonrpc: string;
  id?: number | string;
  result?: unknown;
  error?: { code: number; message: string };
}

let rpcSeq = 0;

/** Minimal JSON Schema -> zod (objects, string/number/integer/boolean/array/enum/null; else unknown). */
export function jsonSchemaToZod(schema: Record<string, unknown>): z.ZodType<unknown> {
  const type = schema.type as string | undefined;
  if (Array.isArray(schema.enum)) {
    // JSON Schema enums may be numbers or mixed — z.enum needs strings.
    const values = schema.enum as unknown[];
    if (values.length && values.every((v) => typeof v === "string")) return z.enum(values as [string, ...string[]]);
    if (values.length) {
      const literals = values.map((v) => z.literal(v as string | number | bigint | boolean | null));
      return literals.length > 1 ? z.union(literals as unknown as [z.ZodTypeAny, z.ZodTypeAny, ...z.ZodTypeAny[]]) : literals[0];
    }
    return z.never();
  }
  switch (type) {
    case "string":
      return z.string();
    case "number":
      return z.number();
    case "integer":
      return z.number().int();
    case "boolean":
      return z.boolean();
    case "null":
      return z.null();
    case "array": {
      const items = schema.items as Record<string, unknown> | undefined;
      return z.array(items ? jsonSchemaToZod(items) : z.unknown());
    }
    case "object": {
      const props = (schema.properties ?? {}) as Record<string, Record<string, unknown>>;
      const required = new Set((schema.required ?? []) as string[]);
      const shape: Record<string, z.ZodType<unknown>> = {};
      for (const [key, sub] of Object.entries(props)) {
        const inner = jsonSchemaToZod(sub);
        shape[key] = required.has(key) ? inner : inner.optional();
      }
      return z.object(shape).passthrough();
    }
    default:
      return z.unknown();
  }
}

async function parseStreamableResponse(res: Response): Promise<JsonRpcResponse> {
  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("text/event-stream")) {
    const text = await res.text();
    let last: JsonRpcResponse | undefined;
    for (const line of text.split("\n")) {
      const t = line.trim();
      if (!t.startsWith("data:")) continue;
      const payload = t.slice(5).trim();
      if (payload === "[DONE]") continue;
      try {
        const msg = JSON.parse(payload) as JsonRpcResponse;
        if (msg.id !== undefined || msg.result !== undefined || msg.error) last = msg;
      } catch {
        continue;
      }
    }
    if (!last) throw new AgentError("MCP_PROTOCOL", "Empty SSE response from MCP server", true);
    return last;
  }
  return (await res.json()) as JsonRpcResponse;
}

function checkRpc(res: JsonRpcResponse, what: string): unknown {
  if (res.error) throw new AgentError("MCP_ERROR", `MCP ${what}: ${res.error.message} (${res.error.code})`, false);
  return res.result;
}

/** MCP client over Streamable HTTP. */
export class McpHttpClient {
  readonly serverId: string;
  private url: string;
  private headers: Record<string, string>;
  private fetchFn: typeof fetch;
  private clientName: string;
  private sessionId?: string;
  private connected = false;

  constructor(url: string, opts: McpClientOptions & { serverId?: string } = {}) {
    this.url = url.replace(/\/$/, "");
    this.serverId = opts.serverId ?? url;
    this.headers = opts.headers ?? {};
    this.fetchFn = opts.fetchFn ?? fetch;
    this.clientName = opts.clientName ?? "framework-agents";
  }

  private async rpc(method: string, params?: unknown): Promise<unknown> {
    return this.rpcInner(method, params, true);
  }

  /** JSON-RPC notification — no id, no response expected. */
  private async notify(method: string, params?: unknown): Promise<void> {
    await this.rpcInner(method, params, false);
  }

  private async rpcInner(method: string, params: unknown | undefined, withId: boolean): Promise<unknown> {
    let res: Response;
    try {
      res = await this.fetchFn(this.url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          accept: "application/json, text/event-stream",
          ...(this.sessionId ? { "mcp-session-id": this.sessionId } : {}),
          ...this.headers,
        },
        body: JSON.stringify(
          withId ? { jsonrpc: "2.0", id: ++rpcSeq, method, params: params ?? {} } : { jsonrpc: "2.0", method, params: params ?? {} },
        ),
      });
    } catch (err) {
      throw new AgentError("MCP_TRANSPORT", `MCP request failed: ${err instanceof Error ? err.message : String(err)}`, true);
    }
    const session = res.headers.get("mcp-session-id");
    if (session) this.sessionId = session;
    if (!res.ok) throw new AgentError("MCP_TRANSPORT", `MCP HTTP ${res.status}`, res.status >= 500);
    if (!withId) {
      // Notifications carry no id: no response will ever match. Read and
      // discard whatever the server sends instead of hanging on it.
      await res.text().catch(() => "");
      return undefined;
    }
    return checkRpc(await parseStreamableResponse(res), method);
  }

  async connect(): Promise<{ name: string; version?: string }> {
    const result = (await this.rpc("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: this.clientName, version: "0.1.0" },
    })) as { serverInfo?: { name: string; version?: string } };
    await this.notify("notifications/initialized", {}).catch(() => undefined);
    this.connected = true;
    return { name: result.serverInfo?.name ?? this.serverId, version: result.serverInfo?.version };
  }

  private requireConnected(): void {
    if (!this.connected) throw new AgentError("MCP_NOT_CONNECTED", "Call connect() first", false);
  }

  async listTools(): Promise<McpToolDef[]> {
    this.requireConnected();
    const result = (await this.rpc("tools/list")) as { tools?: McpToolDef[] };
    return result.tools ?? [];
  }

  async callTool(name: string, args: unknown): Promise<unknown> {
    this.requireConnected();
    const result = (await this.rpc("tools/call", { name, arguments: args ?? {} })) as {
      content?: { type: string; text?: string }[];
      isError?: boolean;
    };
    const text = (result.content ?? [])
      .filter((c) => c.type === "text")
      .map((c) => c.text ?? "")
      .join("\n");
    if (result.isError) throw new AgentError("MCP_TOOL_ERROR", `MCP tool "${name}" failed: ${text}`, false);
    return text;
  }

  async close(): Promise<void> {
    this.connected = false;
    this.sessionId = undefined;
  }
}

/** MCP client over stdio (spawns `command args`, speaks line-delimited JSON-RPC). */
export class McpStdioClient {
  readonly serverId: string;
  private child?: ChildProcess;
  private buffer = "";
  private pending = new Map<number | string, (res: JsonRpcResponse) => void>();
  private clientName: string;

  constructor(
    private command: string,
    private args: string[] = [],
    opts: { serverId?: string; clientName?: string } = {},
  ) {
    this.serverId = opts.serverId ?? `${command} ${args.join(" ")}`.trim();
    this.clientName = opts.clientName ?? "framework-agents";
  }

  async connect(): Promise<void> {
    try {
      this.child = spawn(this.command, this.args, { stdio: ["pipe", "pipe", "ignore"] });
    } catch (err) {
      throw new AgentError("MCP_TRANSPORT", `Cannot spawn MCP server: ${err instanceof Error ? err.message : String(err)}`, false);
    }
    if (!this.child.stdout || !this.child.stdin) {
      throw new AgentError("MCP_TRANSPORT", "MCP stdio pipes unavailable", false);
    }
    this.child.on("error", (err) => {
      for (const [, respond] of this.pending) {
        respond({ jsonrpc: "2.0", error: { code: -32000, message: `MCP server failed: ${err.message}` } });
      }
      this.pending.clear();
    });
    // Spawn succeeded but the process later dies (crash, OOM, exit):
    // without this, in-flight requests hang forever — 'error' never fires.
    this.child.on("exit", (code, signal) => {
      for (const [, respond] of this.pending) {
        respond({ jsonrpc: "2.0", error: { code: -32000, message: `MCP server exited (code ${code ?? "null"}, signal ${signal ?? "none"})` } });
      }
      this.pending.clear();
    });
    this.child.stdout.on("data", (chunk: Buffer) => {
      this.buffer += chunk.toString("utf8");
      const lines = this.buffer.split("\n");
      this.buffer = lines.pop() ?? "";
      for (const line of lines) {
        const t = line.trim();
        if (!t) continue;
        try {
          const msg = JSON.parse(t) as JsonRpcResponse;
          if (msg.id !== undefined) this.pending.get(msg.id)?.(msg);
        } catch {
          continue;
        }
      }
    });
    const result = (await this.request("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: this.clientName, version: "0.1.0" },
    })) as { serverInfo?: { name: string } };
    void result;
  }

  private request(method: string, params: unknown): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (!this.child?.stdin) {
        reject(new AgentError("MCP_NOT_CONNECTED", "Call connect() first", false));
        return;
      }
      const id = ++rpcSeq;
      this.pending.set(id, (res) => {
        this.pending.delete(id);
        try {
          resolve(checkRpc(res, method));
        } catch (err) {
          reject(err);
        }
      });
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`, (err) => {
        if (err) {
          this.pending.delete(id);
          reject(new AgentError("MCP_TRANSPORT", `MCP stdio write failed: ${err.message}`, true));
        }
      });
    });
  }

  async listTools(): Promise<McpToolDef[]> {
    const result = (await this.request("tools/list", {})) as { tools?: McpToolDef[] };
    return result.tools ?? [];
  }

  async callTool(name: string, args: unknown): Promise<unknown> {
    const result = (await this.request("tools/call", { name, arguments: args ?? {} })) as {
      content?: { type: string; text?: string }[];
      isError?: boolean;
    };
    const text = (result.content ?? [])
      .filter((c) => c.type === "text")
      .map((c) => c.text ?? "")
      .join("\n");
    if (result.isError) throw new AgentError("MCP_TOOL_ERROR", `MCP tool "${name}" failed: ${text}`, false);
    return text;
  }

  async close(): Promise<void> {
    this.child?.kill();
    this.child = undefined;
    // Settle in-flight requests — clearing without responding would leave
    // their promises pending forever.
    for (const [, respond] of this.pending) {
      respond({ jsonrpc: "2.0", error: { code: -32000, message: "MCP client closed" } });
    }
    this.pending.clear();
  }
}

export type AnyMcpClient = Pick<McpHttpClient, "serverId" | "listTools" | "callTool" | "close">;

export interface McpClient {
  readonly serverId: string;
  listTools(): Promise<McpToolDef[]>;
  callTool(name: string, args: unknown): Promise<unknown>;
  close(): Promise<void>;
}

/** Turn MCP tool defs into framework Tools (namespaced `server.tool` on collision). */
export async function mcpToolsToFramework(client: McpClient, defs?: McpToolDef[]): Promise<Tool[]> {
  const list = defs ?? (await client.listTools());
  const seen = new Set<string>();
  return list.map((d) => {
    const name = seen.has(d.name) ? `${client.serverId}.${d.name}` : d.name;
    seen.add(d.name);
    return defineTool({
      name,
      description: d.description ?? `MCP tool ${d.name} (via ${client.serverId})`,
      schema: jsonSchemaToZod(d.inputSchema ?? { type: "object" }),
      execute: async (args: unknown) => client.callTool(d.name, args),
    });
  });
}

export async function connectMcpHttp(url: string, opts?: McpClientOptions): Promise<McpHttpClient> {
  const client = new McpHttpClient(url, opts);
  await client.connect();
  return client;
}

export async function connectMcpStdio(command: string, args: string[] = []): Promise<McpStdioClient> {
  const client = new McpStdioClient(command, args);
  await client.connect();
  return client;
}
