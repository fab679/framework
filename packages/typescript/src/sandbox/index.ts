import { runInNewContext } from "node:vm";
import { z } from "zod";
import { AgentError } from "../types/index.js";
import { defineTool, type Tool } from "../tools/index.js";

/**
 * Sandboxed JavaScript execution (node:vm, fresh context per call).
 * Code runs as a program (completion value returned — wrap object literals
 * in parens). No require/fetch/process by default; timeout enforced; output
 * must be JSON-serializable (returned) or a string. For untrusted code
 * prefer an external container — this is a speed bump, not a security boundary.
 */
export function runJavascriptTool(opts: { timeoutMs?: number; maxOutputChars?: number } = {}) {
  const timeoutMs = opts.timeoutMs ?? 2000;
  const maxOutputChars = opts.maxOutputChars ?? 4000;
  return defineTool({
    name: "run_javascript",
    description: "Execute JavaScript in a sandbox (no network, no fs, timeout). Completion value becomes the result.",
    schema: z.object({ code: z.string().min(1).max(8000) }),
    execute: async ({ code }: { code: string }) => {
      let result: unknown;
      try {
        result = runInNewContext(code, Object.freeze({ Math, JSON }), { timeout: timeoutMs });
      } catch (err) {
        // NOTE: vm errors may be cross-realm (instanceof unreliable) — match on message.
        const message = err instanceof Error ? err.message : String(err);
        if (/timed out/i.test(message)) {
          throw new AgentError("SANDBOX_TIMEOUT", `Sandbox exceeded ${timeoutMs}ms`, true);
        }
        throw new AgentError("SANDBOX_ERROR", `Sandbox failed: ${message}`, false);
      }
      const text = typeof result === "string" ? result : JSON.stringify(result ?? null);
      return text.length > maxOutputChars ? `${text.slice(0, maxOutputChars)}…[truncated]` : (result ?? null);
    },
  });
}

import { execFile } from "node:child_process";
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { join, relative, resolve } from "node:path";
import { filesystemTools } from "../middleware/index.js";

/**
 * Sandbox backend (local-first): one rooted workspace with shell `execute`
 * plus file tools, matching the Deep Agents backend shape (ls, read_file,
 * write_file, edit_file, glob, grep + execute).
 *
 * Isolation, honestly scoped: fresh cwd jail per call, scrubbed env (no
 * secrets/proxies inherited), timeouts, output caps with spill-to-file.
 * This is NOT a container boundary: never put secrets in the sandbox env,
 * gate `execute` with human-in-the-loop for sensitive work, and treat all
 * outputs as untrusted input.
 */

export interface SandboxOptions {
  /** Workspace root. Defaults to cwd. */
  root?: string;
  timeoutMs?: number;
  maxOutputChars?: number;
  /** Extra env vars for shell commands (merged over the scrubbed base). */
  env?: Record<string, string>;
}

export interface SandboxBackend {
  readonly root: string;
  tools(): Tool[];
}

const SCRUBBED_ENV = ["PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM", "NO_COLOR"];

function baseEnv(extra: Record<string, string> = {}): Record<string, string> {
  const env: Record<string, string> = {};
  for (const k of SCRUBBED_ENV) {
    const v = process.env[k];
    if (v !== undefined) env[k] = v;
  }
  return { ...env, ...extra };
}

function escapeRegExp(s: string): string {
  return s.replace(/[.+^${}()|[\]\\]/g, "\\$&");
}

/** Minimal glob (*, ?, and double-star for crossing directories) over paths relative to root. */
export function matchGlob(pattern: string, path: string): boolean {
  const parts = pattern.split("**/");
  let rx = "^";
  parts.forEach((part, index) => {
    if (index > 0) rx += "(.*/)?";
    for (let i = 0; i < part.length; i++) {
      const c = part[i];
      if (c === "*" && part[i + 1] === "*") {
        rx += ".*";
        i += 1;
      } else if (c === "*") rx += "[^/]*";
      else if (c === "?") rx += "[^/]";
      else rx += escapeRegExp(c);
    }
  });
  return new RegExp(`${rx}$`).test(path);
}

async function walkFiles(root: string): Promise<string[]> {
  const out: string[] = [];
  const walk = async (dir: string): Promise<void> => {
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const full = join(dir, e.name);
      if (e.isDirectory()) await walk(full);
      else if (e.isFile()) out.push(relative(root, full));
    }
  };
  await walk(root);
  return out.sort();
}

export function createSandboxBackend(opts: SandboxOptions = {}): SandboxBackend {
  const root = resolve(opts.root ?? process.cwd());
  const timeoutMs = opts.timeoutMs ?? 30000;
  const maxOutputChars = opts.maxOutputChars ?? 12000;
  const env = baseEnv(opts.env);
  let spills = 0;

  const spill = async (text: string): Promise<string> => {
    spills += 1;
    const name = `.spill-${spills}.txt`;
    await writeFile(join(root, name), text, "utf8");
    return `${text.slice(0, maxOutputChars)}\n…[truncated ${text.length - maxOutputChars} chars → read ${name} with read_file]`;
  };

  const executeTool = defineTool({
    name: "execute",
    description: "Run a shell command in the sandbox workspace (cwd=root, scrubbed env, timeout). Large output spills to a file.",
    schema: z.object({ command: z.string().min(1).max(4000) }),
    execute: ({ command }: { command: string }): Promise<string> =>
      new Promise<string>((resolveExec, reject) => {
        execFile("sh", ["-c", command], { cwd: root, env, timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 }, (err, stdout, stderr) => {
          if (err && (err as { killed?: boolean }).killed) {
            // killed=true covers TWO cases with different codes; don't report
            // an instant output overflow as a 30s timeout.
            if ((err as { code?: unknown }).code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER") {
              reject(new AgentError("SANDBOX_OUTPUT_TOO_LARGE", "Command output exceeded the 4MB buffer; write output to a file and read it in chunks", false));
              return;
            }
            reject(new AgentError("SANDBOX_TIMEOUT", `Command exceeded ${timeoutMs}ms`, true));
            return;
          }
          const code = err && "code" in err ? (err as { code: number }).code : 0;
          const combined = `${stdout}${stderr ? `\n[stderr]\n${stderr}` : ""}`;
          if (combined.length <= maxOutputChars) {
            resolveExec(err ? `${combined}\n[Command failed with exit code ${code}]` : combined || "[Command succeeded with exit code 0]");
            return;
          }
          void spill(combined).then(
            (pointer) => resolveExec(`${pointer}\n[exit ${code}]`),
            reject,
          );
        });
      }),
  });

  const globTool = defineTool({
    name: "glob",
    description: "List workspace files matching a glob (*, ?, **).",
    schema: z.object({ pattern: z.string().min(1).max(300) }),
    execute: async ({ pattern }: { pattern: string }) => (await walkFiles(root)).filter((f) => matchGlob(pattern, f)).join("\n"),
  });

  const grepTool = defineTool({
    name: "grep",
    description: "Search file contents (literal substring) under a glob. Returns path:line matches.",
    schema: z.object({ query: z.string().min(1).max(300), pattern: z.string().default("**") }),
    execute: async ({ query, pattern }: { query: string; pattern?: string }) => {
      const files = (await walkFiles(root)).filter((f) => matchGlob(pattern ?? "**", f));
      const hits: string[] = [];
      for (const f of files.slice(0, 200)) {
        let info;
        try {
          info = await stat(join(root, f));
        } catch {
          continue;
        }
        if (info.size > 512 * 1024) continue;
        let content: string;
        try {
          content = await readFile(join(root, f), "utf8");
        } catch {
          continue;
        }
        content.split("\n").forEach((line, i) => {
          if (line.includes(query)) hits.push(`${f}:${i + 1}:${line.slice(0, 200)}`);
        });
        if (hits.length >= 50) break;
      }
      return hits.join("\n") || "(no matches)";
    },
  });

  return {
    root,
    tools: () => [...filesystemTools(root), globTool, grepTool, executeTool],
  };
}
