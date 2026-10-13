/**
 * Prompts: versioned, renderable templates. No magic strings in agent code.
 * Sources: inline definePrompt, markdown files with frontmatter, A/B variants.
 */
import { readFileSync } from "node:fs";

export interface PromptTemplate {
  name: string;
  version: string;
  template: string;
  render(vars: Record<string, string | number>): string;
}

export function definePrompt(name: string, template: string, version = "0.1.0"): PromptTemplate {
  return {
    name,
    version,
    template,
    render(vars) {
      return template.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, key: string) => {
        if (!(key in vars)) throw new Error(`Missing prompt var: ${key} (prompt: ${name})`);
        return String(vars[key]);
      });
    },
  };
}

/** Load `--- name, version ---` frontmatter + markdown body from disk. */
export function loadPromptFile(path: string): PromptTemplate {
  const file = readFileSync(path, "utf8");
  // Tolerate a leading BOM and CRLF line endings (frontmatter at EOF is fine).
  const raw = file.startsWith("﻿") ? file.slice(1) : file;
  const match = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!match) throw new Error(`Invalid prompt file (missing frontmatter): ${path}`);
  const meta: Record<string, string> = {};
  for (const line of match[1].split("\n")) {
    const i = line.indexOf(":");
    if (i > 0) meta[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^["']|["']$/g, "");
  }
  if (!meta.name) throw new Error(`Invalid prompt file (missing name): ${path}`);
  return definePrompt(meta.name, match[2].trim(), meta.version ?? "0.1.0");
}

/** A/B variants: pick by traffic split (weighted random) or explicit variant. */
export function abPrompts(
  variants: { name: string; template: string; version?: string; weight?: number }[],
  pick?: string,
): PromptTemplate {
  if (pick) {
    const found = variants.find((v) => (v.version ? `${v.name}@${v.version}` : v.name) === pick || v.name === pick);
    if (!found) throw new Error(`Unknown prompt variant: ${pick}`);
    return definePrompt(found.name, found.template, found.version);
  }
  const total = variants.reduce((n, v) => n + (v.weight ?? 1), 0);
  let roll = Math.random() * total;
  for (const v of variants) {
    roll -= v.weight ?? 1;
    if (roll <= 0) return definePrompt(v.name, v.template, v.version);
  }
  const last = variants[variants.length - 1];
  return definePrompt(last.name, last.template, last.version);
}
