# Sandbox & rag & skills

Execution isolation, retrieval, portable capability packs, and prompt templates — same shapes in both languages (Python names snake_case: `create_sandbox_backend`, `define_prompt`, ...).

## createSandboxBackend

A rooted workspace backend (the Deep Agents shape): `ls`, `read_file`, `write_file`, `edit_file`, plus `glob`, `grep`, and `execute` — one shell inside the root.

```ts
import { createSandboxBackend, runJavascriptTool } from "framework-agents";

const sandbox = createSandboxBackend({ root: "./workspace", timeoutMs: 30000, maxOutputChars: 12000, env: {} });
// defaults: timeoutMs 30s, maxOutputChars 12000
const agent = new Agent({ model, tools: [...sandbox.tools(), ...] });
```

- **`execute`** runs `sh -c <command>` jailed to `cwd: root`. The env is **scrubbed** — only `PATH, HOME, LANG, LC_ALL, TZ, TERM, NO_COLOR` (plus your explicit `env`) are inherited, so no secrets or proxy settings leak into commands.
- **Timeouts:** past `timeoutMs` the child is killed and re-raises `SANDBOX_TIMEOUT` (retryable). Output past the 4MB pipe buffer raises `SANDBOX_OUTPUT_TOO_LARGE` (not a timeout) with the fix in the message: write to a file and read it in chunks.
- **Output caps + spill files:** output over `maxOutputChars` is written to `.spill-<n>.txt` in the root; the result is the truncated text plus a pointer (`…[truncated N chars → read .spill-1.txt with read_file]`) so the model can page through it.
- **`glob`** supports `*`, `?`, and `**`; **`grep`** is literal-substring search under a glob, returning `path:line` matches capped at 50.
- Python: `create_sandbox_backend(root, timeout_ms=30000, max_buffer=4MB, max_output_chars=12000, env=None)` — same tool set, with output caps enforced **incrementally** (the child is killed the moment accumulated output crosses the cap, not at a fixed 4MB).

### runJavascriptTool

```ts
const js = runJavascriptTool({ timeoutMs: 2000, maxOutputChars: 4000 });
// tool name: run_javascript, schema: { code: string } (1..8000 chars)
```

Executes model-written JavaScript (TS: `node:vm` with a fresh frozen context — completion value is the result, so wrap object literals in parens; Python: `exec` with a restricted builtin set, result = last expression + printed output). Only `Math`/`JSON` and limited builtins are exposed: no require, fetch, fs, or process. Timeouts throw `SANDBOX_TIMEOUT` (retryable), everything else `SANDBOX_ERROR`.

### Caveats (honestly scoped)

This is a **speed bump, not a security boundary** — never a container boundary. Never put secrets in the sandbox env, gate `execute` with [human-in-the-loop](middleware.md) for sensitive work, and treat all outputs as untrusted input. For genuinely untrusted code, prefer an external container. Known Python limitation: `exec` timeouts cannot hard-kill a thread mid-statement — the surfaced error honors the contract, but the abandoned thread dies at interpreter exit.

## Retrieval

`Retriever` is a two-method contract: readonly `id` and `search(query, k?)` → `ScoredDoc[]` (`{ id, text, score, meta? }`). No bundled vector DB — adapters only:

```ts
import { InMemoryRetriever, retrievalTool } from "framework-agents";

const retriever = new InMemoryRetriever([
  { id: "kb-1", text: "Return policy: 30 days, receipt required." },
]);
const agent = new Agent({
  model,
  tools: [retrievalTool(retriever, { k: 4, name: "retrieve" })],
});
```

- **`InMemoryRetriever`** — dependency-free TF-IDF over static documents (k defaults to 4).
- **`retrievalTool(retriever, { k?, name? })`** — wraps any retriever as a framework tool; the schema is `{ query, k }` with `k` clamped 1–10 (default from the `k` option).
- **`sparqlRetriever(store)`** — keyword match over the graph closure rendered as text triples; see [semantic graph](semantic.md).

Python: `InMemoryRetriever(docs)`, `retrieval_tool(retriever, k=4, name="retrieve")`, `rag.py` mirrors the shapes.

## Skills

A skill is a directory with a **`SKILL.md`** (frontmatter: `name`, `description`, `version` default `0.1.0` + a markdown body). The loader discovers, validates, and exposes skills; activation is explicit, never magic:

- **`discoverSkills(dirs, { strict? })`** — scans directories for `SKILL.md` (one level of nesting); invalid skills are skipped (or throw with `strict: true`); missing directories never throw.
- **`parseSkillFile(content, path?)`** — pure frontmatter/body parse (BOM- and CRLF-tolerant) as the test seam.
- **`SkillRegistry`** — `register/get/list` (duplicate names throw) and `asTools()` for contributed tools.
- **`loadSkillsInto(registry, dirs)`** — load + register everything found.

**`$name` mention routing:** `matchSkills(registry, input, threshold = 0.05)`/`scoreSkills` TF-IDF-score name+description, but an explicit **whole-word `$name` mention always wins** (word-boundary match — `$reference` does not fire on `$references`), then higher scorer above the threshold. No embedding APIs, no network.

Python: `discover_skills`, `parse_skill_file`, `SkillRegistry`, `match_skills`/`score_skills` (same `$mention` word-boundary rule). See [A2A serving](a2a-serving.md) for how `framework.json` wires discovered skills into a served agent's system prompt.

## Prompts

Versioned, renderable templates — no magic strings in agent code (all also in Python: `define_prompt`, `load_prompt_file`, `ab_prompts`):

- **`definePrompt(name, template, version?)`** — `template.render(vars)` interpolates `{{ var }}`; a missing var throws (`Missing prompt var`).
- **`loadPromptFile(path)`** — markdown file with `--- name: / version: ---` frontmatter (BOM/CRLF tolerant).
- **`abPrompts(variants, pick?)`** — A/B variants picked by weighted random traffic split, or explicitly by `name` / `name@version`. `render` conforms to the same template rules.

```ts
const greeting = definePrompt("greeting", "Hello {{ name }}!", "1.0.0");
greeting.render({ name: "world" });
const picked = abPrompts([
  { name: "casual", template: "Hey {{ name }}!", weight: 2 },
  { name: "formal", template: "Dear {{ name }},", weight: 1 },
]);
```

## Related

- [Middleware & HITL](middleware.md) — gating `execute` behind human approval
- [Semantic graph](semantic.md) — the graph-backed retriever and GraphMemory
- [Live examples](../getting-started/live-examples.md) — sandbox (`13`), rag (`10`), skills/prompts (`14`) demos