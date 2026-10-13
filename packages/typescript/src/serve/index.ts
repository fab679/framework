import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { config as dotenvConfig } from "dotenv";
import { z } from "zod";
import { Agent } from "../agent/index.js";
import { initChatModel } from "../models/factory.js";
import { A2AServer } from "../a2a/index.js";
import { SessionManager } from "../sessions/index.js";
import { LocalFileStorage } from "../sessions/index.js";
import { discoverSkills, SkillRegistry } from "../skills/index.js";
import { FileCheckpointer } from "../checkpoint/index.js";
import { filesystemMiddleware, todoListMiddleware } from "../middleware/index.js";
import type { AgentMiddleware } from "../middleware/index.js";

/**
 * framework.json — declare agents, launch them as A2A servers.
 * Relative paths resolve against the config file's directory.
 */

const AgentDefSchema = z.object({
  name: z.string().min(1),
  description: z.string().default(""),
  systemPrompt: z.string().default("You are a helpful agent. Use tools when needed. Be concise."),
  model: z.string(),
  port: z.number().int().positive().optional(),
  skills: z.array(z.string()).default([]),
  filesystem: z.string().optional(),
  todos: z.boolean().default(false),
  maxSteps: z.number().int().positive().default(8),
  sessionDir: z.string().optional(),
  /** Extra env file for this agent (keys override framework-level). */
  envFile: z.string().optional(),
});

/**
 * An agent entry is either inline or a file reference:
 * `{ "file": "./agents/helper.json" }` (plus optional inline overrides).
 */
const AgentEntrySchema = z.union([
  AgentDefSchema,
  z.object({ file: z.string() }).catchall(z.unknown()),
]);

const FrameworkFileSchema = z.object({
  server: z.object({ host: z.string().default("127.0.0.1"), basePort: z.number().int().min(0).default(8484) }).default({}),
  /** Framework-level env file(s), loaded before agents resolve models. */
  envFile: z.union([z.string(), z.array(z.string())]).optional(),
  /** Inline env mapping (applied after env files, overrides them). */
  env: z.record(z.string()).optional(),
  /** Local checkpoint dir for served agents (durable HITL threads). */
  checkpointer: z.object({ dir: z.string() }).optional(),
  http: z.object({ corsOrigins: z.array(z.string()).default([]) }).default({}),
  agents: z.array(AgentEntrySchema).min(1),
});

export { FrameworkFileSchema };

export type FrameworkFile = z.infer<typeof FrameworkFileSchema>;
export type ServedAgent = { name: string; url: string; cardUrl: string; server: A2AServer };
export type ResolvedAgentDef = z.infer<typeof AgentDefSchema>;

/** loadFrameworkConfig output: every entry resolved to a concrete agent def. */
export interface LoadedFrameworkFile {
  server: { host: string; basePort: number };
  envFile?: string | string[];
  env?: Record<string, string>;
  checkpointer?: { dir: string };
  http: { corsOrigins: string[] };
  agents: ResolvedAgentDef[];
}

/**
 * Load an env file if present (silent when missing). `override: false` fills
 * gaps like plain dotenv (the process environment wins); `override: true`
 * lets later files beat earlier ones — used for agent-level files, which the
 * config schema documents as overriding framework-level keys.
 */
export function loadEnvFile(path: string, override = true): void {
  dotenvConfig({ path, override });
}

function resolveAgentPaths(
  def: z.infer<typeof AgentDefSchema>,
  dir: string,
): ResolvedAgentDef {
  return {
    ...def,
    skills: def.skills.map((s) => resolve(dir, s)),
    ...(def.filesystem ? { filesystem: resolve(dir, def.filesystem) } : {}),
    ...(def.sessionDir ? { sessionDir: resolve(dir, def.sessionDir) } : {}),
    ...(def.envFile ? { envFile: resolve(dir, def.envFile) } : {}),
  };
}

/** Merge file-owned keys (resolved against the agent file's dir) with inline overrides (framework dir). */
function resolveAgentEntry(
  fileRaw: Record<string, unknown>,
  overrides: Record<string, unknown>,
  agentDir: string,
  frameworkDir: string,
): ResolvedAgentDef {
  const fileKeys = new Set(Object.keys(fileRaw));
  const merged = AgentDefSchema.parse({ ...fileRaw, ...overrides });
  const against = (key: string): string => (fileKeys.has(key) && !(key in overrides) ? agentDir : frameworkDir);
  return {
    ...merged,
    skills: merged.skills.map((s) => resolve(against("skills"), s)),
    ...(merged.filesystem ? { filesystem: resolve(against("filesystem"), merged.filesystem) } : {}),
    ...(merged.sessionDir ? { sessionDir: resolve(against("sessionDir"), merged.sessionDir) } : {}),
    ...(merged.envFile ? { envFile: resolve(against("envFile"), merged.envFile) } : {}),
  };
}

export function loadFrameworkConfig(path: string): LoadedFrameworkFile {
  const raw = JSON.parse(readFileSync(path, "utf8")) as unknown;
  const parsed = FrameworkFileSchema.parse(raw);
  const dir = dirname(resolve(path));

  for (const envFile of parsed.envFile ? [parsed.envFile].flat() : []) {
    // Framework-level files fill gaps only — real environment variables win.
    loadEnvFile(resolve(dir, envFile), false);
  }
  for (const [key, value] of Object.entries(parsed.env ?? {})) {
    process.env[key] = value;
  }

  return {
    server: parsed.server,
    ...(parsed.envFile ? { envFile: parsed.envFile } : {}),
    ...(parsed.env ? { env: parsed.env } : {}),
    ...(parsed.checkpointer ? { checkpointer: { dir: resolve(dir, parsed.checkpointer.dir) } } : {}),
    http: parsed.http,
    agents: parsed.agents.map((entry) => {
      if ("file" in entry && typeof entry.file === "string") {
        const agentPath = resolve(dir, entry.file);
        const agentDir = dirname(agentPath);
        const fileRaw = JSON.parse(readFileSync(agentPath, "utf8")) as Record<string, unknown>;
        const { file: _file, ...overrides } = entry as Record<string, unknown>;
        void _file;
        const resolved = resolveAgentEntry(fileRaw, overrides, agentDir, dir);
        if (resolved.envFile) loadEnvFile(resolved.envFile);
        return resolved;
      }
      const resolved = resolveAgentPaths(AgentDefSchema.parse(entry), dir);
      if (resolved.envFile) loadEnvFile(resolved.envFile);
      return resolved;
    }),
  };
}

export async function buildServedAgent(
  def: ResolvedAgentDef,
  opts: { sessionId?: string } = {},
): Promise<{ agent: Agent; card: { name: string; description?: string } }> {
  const middlewares: AgentMiddleware[] = [];
  if (def.filesystem) middlewares.push(filesystemMiddleware({ root: def.filesystem }));
  if (def.todos) middlewares.push(todoListMiddleware());

  let systemPrompt = def.systemPrompt;
  if (def.skills.length) {
    const registry = new SkillRegistry();
    for (const dir of def.skills) {
      for (const s of await discoverSkills([dir])) {
        try {
          registry.register(s);
        } catch {
          // Duplicate skill names across dirs: first wins.
        }
      }
    }
    const catalog = registry.list().map((s) => `- ${s.name}: ${s.description}`).join("\n");
    if (catalog) systemPrompt += `\n\nAvailable skill packs (mention $name to invoke):\n${catalog}`;
  }

  const agent = new Agent({
    name: def.name,
    description: def.description,
    systemPrompt,
    model: initChatModel(def.model),
    maxSteps: def.maxSteps,
    middleware: middlewares,
    ...(def.sessionDir
      ? {
          // Context-scoped id keeps one def's sessions from mingling when
          // the server runs a fresh agent per A2A contextId.
          sessionManager: new SessionManager({
            sessionId: opts.sessionId ?? def.name,
            storage: new LocalFileStorage(def.sessionDir),
          }),
        }
      : {}),
  });
  return { agent, card: { name: def.name, ...(def.description ? { description: def.description } : {}) } };
}

export async function serveFramework(file: LoadedFrameworkFile): Promise<{ agents: ServedAgent[]; stop: () => Promise<void> }> {
  const served: ServedAgent[] = [];
  const checkpointer = file.checkpointer ? new FileCheckpointer(file.checkpointer.dir) : undefined;
  let port = file.server.basePort;
  for (const def of file.agents) {
    const { card } = await buildServedAgent(def);
    const server = new A2AServer({
      card,
      // Fresh agent per A2A contextId — sharing one instance would leak one
      // conversation into another context's history.
      agentFactory: (contextId) =>
        buildServedAgent(def, def.sessionDir ? { sessionId: `${def.name}:${contextId}` } : {}).then((r) => r.agent),
      ...(checkpointer ? { checkpointer } : {}),
      corsOrigins: file.http.corsOrigins,
      host: file.server.host,
    });
    const actual = await server.start(def.port ?? port, file.server.host);
    port = actual + 1;
    const url = `http://${file.server.host}:${actual}`;
    served.push({ name: def.name, url, cardUrl: `${url}/.well-known/agent-card.json`, server });
  }
  return {
    agents: served,
    stop: async () => {
      for (const s of served) await s.server.stop();
    },
  };
}
