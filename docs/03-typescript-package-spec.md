# 03 — TypeScript Package Spec (source of truth)

Package: `framework-agents` — import only from `framework-agents` (deep imports are unsupported).

## `Agent`

```ts
new Agent({
  name?: string;            // default "agent"
  systemPrompt?: string;
  model: ModelProvider;     // required
  tools?: Tool[];           // default []
  memory?: Memory;
  maxSteps?: number;        // default 6
  events?: AgentEvents;
})
await agent.run(input: string, signal?: AbortSignal): Promise<AgentResult>
```

`AgentResult = { text, messages, toolCalls: { id, name, args, output }[], usage: { inputTokens, outputTokens, reasoningTokens? } }`

## `ChatMessage` (content-block standard)

```ts
content: string | ContentBlock[]  // strings for simple cases, blocks for rich payloads
type ContentBlock =
  | { type: "text"; text: string }
  | { type: "reasoning"; reasoning: string }
  | { type: "image"; url?; data?; mimeType?; fileId? }
  | { type: "audio" | "file"; ... }
  | { type: "tool_call"; id: string; name: string; args: unknown }
  | { type: "non_standard"; value: unknown };  // provider escape hatch

system(content) / user(content, { name? }) / assistant(content) / toolResult(toolCallId, toolName, content)
textOf(content) / hasImages(content) / imagesOf(content)
serializeMessages(msgs) / deserializeMessages(raw)  // validated persistence round-trip
```

Images on a model without vision support fail fast with `AgentError("MODEL_CAPABILITY")` before any network call (see `src/models/capabilities.ts`).

## `Tool`

```ts
defineTool({ name, description, schema: z.ZodType, execute: (args) => Promise<output> })
new ToolRegistry().register(tool).call(name, rawArgs) // parses with zod, throws on bad args
```

## `Memory`

```ts
interface Memory { load(): Promise<ChatMessage[]>; save(msgs): Promise<void>; clear(): Promise<void> }
new InMemoryMemory(maxMessages = 50)
```

## `ModelProvider`

```ts
interface ModelProvider {
  readonly id: string;
  complete(opts: { messages: ChatMessage[]; tools: { name; description }[] }): Promise<{
    text: string; toolCalls: { name; args }[]; usage: TokenUsage;
  }>;
}
new MockModelProvider() // deterministic, for tests/examples
```

## `Runtime`

```ts
runWithBudget(fn, { signal?, timeoutMs?, maxSteps? })
```

## State (three lifetimes)

```ts
new Agent({ state: { theme: "dark" }, initialMessages: [...], conversationManager, sessionManager })
agent.state.get/set/delete          // validated JSON KV, never sent to the model
agent.messages                      // history from the last run
await agent.run("...", { invocationState: { user_id: "u1" } })  // per-run, shared by ref, returned on result
// tools receive (args, { agentName, state, invocationState })
```

## Conversation managers

```ts
new NullConversationManager()                       // keep everything
new SlidingWindowConversationManager({ windowSize: 40, pinFirst: 1, perTurn: true })
new SummarizingConversationManager({ summaryRatio: 0.3, preserveRecent: 10, summarizer? | model? })
```

## Sessions + snapshots

```ts
new SessionManager({ sessionId, storage: new LocalFileStorage("./sessions") })  // or InMemoryStorage
agent.takeSnapshot({ preset: "session", include?, exclude?, appData? })
await agent.loadSnapshot(snapshot)  // restores present fields only
await manager.saveSnapshot(data) / listSnapshotIds() / restoreSnapshot(id) / deleteSession()
```

## Multi-agent

```ts
await runOrchestrated({ strategy: "sequential" | "parallel" | "router", agents }, input, { invocationState })
await runWorkflow(nodes, { invocationState })       // nodes get (ctx, invocationState)
new Swarm({ agents, entryPoint?, maxHandoffs?, maxIterations?, executionTimeoutMs?, nodeTimeoutMs?, loopDetectionWindow?, minUniqueAgents? })
await swarm.run(task, { sharedState })              // sharedState = cross-agent invocationState
for await (const e of swarm.stream(task)) {}        // node_start/done, handoff, agent_event, result
```

## A2A interop

```ts
const client = new A2AClient("http://remote:9000");
await client.getCard(); await client.send("hi", "ctx-1");
a2aAsTool(client)                                   // remote agent as a framework Tool
const server = new A2AServer({ card, agentFactory: (ctx) => new Agent({...}) });
await server.start(port);                           // card + message/send, per-context isolation
```

## `AgentEvents`

```ts
{ onStep?, onToolCall?, onToolResult?, onDone?, onToken?, onThinking? }
consoleEvents(prefix?) // dev logger (streams tokens/thinking to stdout)
```

## `agent.stream()` (event streaming)

```ts
for await (const e of agent.stream("...")) // step | token | thinking | tool_call | tool_result | done
import { collectStream } from "framework-agents"; // { events, result }
```

## Structured output (`responseFormat`)

```ts
new Agent({ model, responseFormat: toolStrategy(ContactSchema) })      // any tool-calling model
new Agent({ model, responseFormat: providerStrategy(ContactSchema) })  // native response_format
// result.structuredResponse — validated; tool strategy self-repairs via
// validation tool messages; provider strategy retries bad JSON in-loop.
```

## Middleware

```ts
new Agent({ model, middleware: [modelCallLimit(10), toolRetry({ attempts: 3 })] })
// hooks: beforeModel / wrapModel / afterModel / beforeTool / aroundTool / afterTool
// order: before_* in order, after_* reversed, wrap_* nested (first = outermost)
// middleware may contribute tools (merged into the agent's list)
```

Prebuilt: `toolRetryMiddleware` (backoff+jitter/filter/scoping/onFailure), `modelRetryMiddleware`,
`modelFallbackMiddleware(...providers)`, `modelCallLimit`, `toolCallLimitMiddleware`,
`humanInTheLoopMiddleware({ interruptOn, decide? })` (approve/edit/reject/suspend),
`summarizationMiddleware({ model, triggerTokens, keepMessages })`,
`piiMiddleware(type, { strategy: redact|mask|hash|block, detector? })`,
`contextEditingMiddleware({ edits: [new ClearToolUsesEdit(...)] })`,
`todoListMiddleware()` (provides `write_todos`), `llmToolSelectorMiddleware({ model })`,
`toolEmulatorMiddleware({ model })`, `filesystemMiddleware({ root })` (ls/read/write/edit),
`createSubAgentMiddleware({ model, subagents })` (provides `task`).
`stampRetryable(err, bool)` / `getRetryable(err)` control retry verdicts.

## Checkpointed HITL resume (local-first)

```ts
// suspend: decide returns { suspend: true } (or omit decide) with checkpointer+threadId
await agent.run("ship it", { checkpointer: new FileCheckpointer("./runs"), threadId: "rel-1" });
// → throws HitlInterrupt { interruptId, tool, args }; checkpoint persisted locally
await agent.resume("rel-1", { [hitlKey("deploy", { env: "prod" })]: "approve" }, { checkpointer });
// withFileLock(path, fn) — local exclusive locks (stale reclaim); no distributed locks by design
```

## Skills routing

`matchSkills(registry, input)` — local TF-IDF cosine over name+description, `$name` mentions always win. No embedding APIs; `scoreSkills` is the seam for a pluggable embedder later.

## Models

```ts
initChatModel("together:Qwen/Qwen3.5-9B")  // bare id defaults to together
initChatModel("openai:gpt-4o-mini")        // OPENAI_API_KEY
initChatModel("anthropic:claude-sonnet-4-6", { thinkingBudget: 5000 })  // native API, thinking blocks
initChatModel("ollama:qwen2.5")            // local-first, no key (OLLAMA_BASE_URL)
initChatModel("custom:local", { baseUrl, apiKey })
```
Anthropic has no native json_schema mode → `auto` responseFormat falls back to the tool strategy. Capability research (vision/structured/streaming per model) lives in `src/models/capabilities.ts`.

## Skills discovery, MCP, semantic store

```ts
await discoverSkills(["./skills"])            // SKILL.md scan
matchSkills(registry, input)                  // TF-IDF routing, $mention wins
await connectMcpHttp(url) / connectMcpStdio(cmd)  // listTools/callTool
await mcpToolsToFramework(client)             // MCP tools -> framework Tools
await store.add(quads)                        // RDFS entailment to fixpoint
await store.why(quad)                         // { premises, rule } proof
```

Quad identity includes the named graph (`match(s, p, o, g)` filters it), so
the same triple can live in several graphs.

## Semantic scale (P2-sem)

```ts
// Remote SPARQL (SELECT always; UPDATE is opt-in at the tool level)
const remote = new RemoteSparqlAdapter({ endpoint, auth, prefixes });
await remote.select("SELECT ?x WHERE { ... }");   // SPARQL 1.1 protocol, results+json
await remote.update("INSERT DATA { ... }");       // application/sparql-update

// Entity linking: mention -> canonical IRI via labels + owl:sameAs groups
const linker = new EntityLinker(store);
await linker.link("Ada", k);                       // [{ iri, label, score, sameAsGroup }]
// Autonomous-by-default mapping proposals (provenance mandatory):
const proposals = new MappingProposals({ store });           // commits on propose
const review = new MappingProposals({ autonomy: false });    // queues for decide()
await review.decide(store, id, "approved");        // human promotes to skos:altLabel

// JSON-LD I/O (named graphs round-trip as {"@id": g, "@graph": [...]})
quadsToJsonLd(quads); jsonldToQuads(doc);

// Ontology evolution: pre-battery -> rebuild -> post-battery; old store intact
const { report, store: v4 } = await migrateTBox(v3, {
  from: "v3", to: "v4",
  renames: [{ from: "ex:worksAt", to: "ex:worksFor" }],
  removed: [{ subject: "...", predicate: "...", object: "..." }],
  added: [/* axioms with source */],
  deprecations: [{ term: "ex:worksAt", replacement: "ex:worksFor", reason: "..." }],
}, { battery: [{ id: "persons-exist", ask: "SELECT ?s WHERE { ?s rdf:type ex:Person . }" }] });
// v4 TBox axioms land in the named graph report.tboxGraph (default urn:framework:ontology:<to>)

// Self-review: the agent audits its own ontology
await reviewOntology(store);        // { classes, properties, undeclared, unused,
                                   //   unlabeled, missingDomain, missingRange }
const proposals = await proposeShapes(store, { minInstances: 2, coverage: 0.5 });
const { adopted, rejected } = await adoptShapes(store, proposals);
// proposals are INDUCED from data; adoption validates the current closure
// first (shapes the data already violates are rejected 1:1, not registered).
// Tools: reviewOntologyTool(store), proposeShapesTool(store, opts).
```

## Guarantees
- Unknown tool → `role: tool` message `Unknown tool: <name>`, loop continues.
- Bad tool args → zod throws before `execute` (via `ToolRegistry.call`).
- `signal.abort()` / `timeoutMs` → `AbortError` / `AgentError("TIMEOUT")`.
- `memory.save` is called once per `run` (excluding system message).
