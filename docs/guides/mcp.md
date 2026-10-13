# MCP clients

Connect any Model Context Protocol server — Streamable HTTP or stdio — and bridge its tools into the framework. Both languages expose the same four pieces:

| Piece | TypeScript | Python |
|---|---|---|
| HTTP client | `McpHttpClient` / `connectMcpHttp(url, opts?)` | `McpHttpClient` / `connect_mcp_http(url, headers=None, ...)` |
| stdio client | `McpStdioClient` / `connectMcpStdio(command, args)` | `McpStdioClient` / `connect_mcp_stdio(command, args)` |
| Bridge | `mcpToolsToFramework(client, defs?)` | `await mcp_tools_to_framework(client, defs=None)` |
| Schema conversion | `jsonSchemaToZod(schema)` | `json_schema_to_model` (pydantic, dynamic) |

## Connecting

```ts
import { connectMcpHttp, connectMcpStdio, mcpToolsToFramework } from "framework-agents";

const http = await connectMcpHttp("https://mcp.example.com/mcp", { headers: { authorization: `Bearer ${k}` } });
const stdio = await connectMcpStdio("npx", ["-y", "some-mcp-server"]);

const tools = await mcpToolsToFramework(http);   // Defs fetched via listTools() by default
await http.close();
```

Both transports speak line/HTTP JSON-RPC with `id` correlation, initialize with a versioned `clientInfo` object (override the client name with `clientName` / `client_name`; the Python port identifies itself as `framework-agents`) and `protocolVersion: "2024-11-05"`, and re-use any `mcp-session-id` header the server assigns.

## Calls, tools, and notifications

- `listTools()` → `McpToolDef[]` (`name`, `description?`, `inputSchema`), `callTool(name, args?)` → the joined text parts of the result (`content` items with `type: "text"`); `isError: true` raises `AgentError("MCP_TOOL_ERROR", ..., retryable=false)`.
- Errors: transport faults → `MCP_TRANSPORT` (retryable on 5xx); RPC errors → `MCP_ERROR`; calling before `connect()` → `MCP_NOT_CONNECTED`. Protocol errors on SSE-only responses (`MCP_PROTOCOL`) are retryable.
- **Notification no-id detail:** `notifications/initialized` is sent as a true JSON-RPC notification — **no `id` on the envelope, and the response body is read and discarded**. Streamable-HTTP servers answer notifications with 202/SSE like any request; a client waiting to correlate a response on a no-id message would hang forever.

## Bridging: schemas and namespacing

`mcpToolsToFramework` converts every tool's JSON-Schema `inputSchema` into a framework tool:

- Unknown-name/repeated names: the second occurrence becomes `serverId.toolName` (`client.serverId` defaults to the URL or the command line) — name collisions never silently overwrite.
- TS converts JSON Schema (type/enum/object/array; `required` → non-optional, extra keys passthrough) into zod; Python builds a dynamic pydantic model (enums become `Literal`).
- **Per-tool degradation (Python):** a tool whose schema cannot be bridged degrades to a permissive schema instead of crashing — one bad tool never takes down the whole server's tool set. Python also serializes only fields the model actually set (`model_fields_set` + `exclude_none`): an optional arg omitted by the caller is absent from the wire payload (never sent as an explicit null — the TS zod shape omits unset keys too).
- The execution bridge calls `client.callTool(originalName, args)` — the original, un-namespaced name.

## Example (from the TS test suite)

The unit suite (`tests/unit/stubs.test.ts`) runs a real stdio child process — an inline Node script that answers `initialize`, `tools/list`, and `tools/call`:

```ts
import { McpStdioClient, mcpToolsToFramework } from "framework-agents";

// A minimal in-memory MCP server over stdio (line-delimited JSON-RPC):
const serverJs = `
let b="";process.stdin.on("data",c=>{b+=c;
  const l=b.split("\\n");b=l.pop();
  for(const t of l){ if(!t.trim())continue;
    const m=JSON.parse(t);
    const r=(x)=>process.stdout.write(JSON.stringify({jsonrpc:"2.0",id:m.id,result:x})+"\\n");
    if(m.method==="initialize") r({serverInfo:{name:"stdio-fake"}});
    else if(m.method==="tools/list") r({tools:[{name:"ping",description:"p",inputSchema:{type:"object"}}]});
    else if(m.method==="tools/call") r({content:[{type:"text",text:"pong"}]});
  }});
`;
const stdio = new McpStdioClient(process.execPath, ["-e", serverJs]);
await stdio.connect();
(await stdio.listTools()).map((t) => t.name);     // ["ping"]
await stdio.callTool("ping", {});                  // "pong"
const [tool] = await mcpToolsToFramework(stdio);   // framework Tool wrapping ping
await stdio.close();
```

Stdio lifecycle notes baked into both clients: a spawned process that later **exits** (crash, OOM, kill) rejects every pending request (an `error` event alone never fires for a clean exit — in-flight calls would otherwise hang), and `close()` settles any in-flight requests with an error instead of dropping their promises.

## Wiring into an agent

```ts
const client = await connectMcpStdio("npx", ["-y", "some-mcp-server"]);
const agent = new Agent({
  model: initChatModel("together:Qwen/Qwen3.5-9B"),
  tools: [...await mcpToolsToFramework(client)],
});
```

The bridged tools are ordinary `Tool`s: argument schemas reach the provider wire, outputs flow back as tool messages, and all [middleware](middleware.md) (HITL gating, limits, retry) applies. The live demo `examples/live/16-mcp-bridge.ts` drives a real stdio server with a live model.

## Related

- [Agents & tools](agents-and-tools.md) — the `Tool` contract these become
- [A2A & serving](a2a-serving.md) — the other interop direction (framework ↔ framework)