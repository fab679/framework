import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import type { Tool } from "../tools/index.js";

/**
 * Skills: portable capability packs (SKILL.md + optional tools/resources).
 * Spec: a skill is a directory with SKILL.md frontmatter
 * (name, description, version) + markdown body. Loader discovers,
 * validates, and exposes skills as Tools or prompt injections.
 */

export interface Skill {
  name: string;
  description: string;
  version: string;
  path: string;
  body: string;
  tools?: Tool[];
}

export interface SkillLoaderOptions {
  /** Directories to scan for SKILL.md files. */
  dirs: string[];
  /** If true, throw on invalid skill; else skip with warning. Default false. */
  strict?: boolean;
}

const FrontmatterSchema = z.object({
  name: z.string().min(1),
  description: z.string().min(1),
  version: z.string().default("0.1.0"),
});

/** Parse SKILL.md content -> { meta, body }. Pure, testable, no FS. */
export function parseSkillFile(content: string, path = "<inline>"): Omit<Skill, "tools"> {
  // Tolerate a leading BOM and CRLF line endings (frontmatter at EOF is fine).
  const raw = content.startsWith("﻿") ? content.slice(1) : content;
  const match = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!match) throw new Error(`Invalid SKILL.md (missing frontmatter): ${path}`);
  const [, fm, body] = match;
  const meta: Record<string, string> = {};
  for (const line of fm.split("\n")) {
    const i = line.indexOf(":");
    if (i > 0) meta[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^["']|["']$/g, "");
  }
  const parsed = FrontmatterSchema.parse(meta);
  if (!body.trim()) throw new Error(`Invalid SKILL.md (empty body): ${path}`);
  return { ...parsed, path, body: body.trim() };
}

/** Registry of loaded skills — activation is explicit, never magic. */
export class SkillRegistry {
  private skills = new Map<string, Skill>();

  register(skill: Skill): this {
    if (this.skills.has(skill.name)) throw new Error(`Skill already registered: ${skill.name}`);
    this.skills.set(skill.name, skill);
    return this;
  }

  get(name: string): Skill | undefined {
    return this.skills.get(name);
  }

  list(): Skill[] {
    return [...this.skills.values()];
  }

  /** Expose each skill's tools (if any) for Agent consumption. */
  asTools(): Tool[] {
    return this.list().flatMap((s) => s.tools ?? []);
  }
}

/**
 * Scan directories for SKILL.md files (one level, plus direct files).
 * Invalid skills are skipped (or throw when `strict`). Never throws for
 * missing directories — returns what it finds.
 */
export async function discoverSkills(dirs: string[], opts: { strict?: boolean } = {}): Promise<Skill[]> {
  const found: Skill[] = [];
  for (const dir of dirs) {
    let entries: string[];
    try {
      entries = readdirSync(dir);
    } catch {
      continue;
    }
    for (const entry of entries) {
      const full = join(dir, entry);
      let stat;
      try {
        stat = statSync(full);
      } catch {
        continue;
      }
      const file = stat.isDirectory() ? join(full, "SKILL.md") : full;
      if (!file.endsWith("SKILL.md")) continue;
      try {
        found.push({ ...parseSkillFile(readFileSync(file, "utf8"), file), tools: [] });
      } catch (err) {
        if (opts.strict) throw err;
      }
    }
  }
  return found;
}

/** Load + register every discoverable skill. Duplicate names throw. */
export async function loadSkillsInto(registry: SkillRegistry, dirs: string[], opts: { strict?: boolean } = {}): Promise<Skill[]> {
  const skills = await discoverSkills(dirs, opts);
  for (const s of skills) registry.register(s);
  return skills;
}

/**
 * Local skill routing — dependency-free TF-IDF cosine over skill
 * name+description. Explicit `$name` mentions always win. No embedding
 * APIs, no network. A pluggable embedder can replace `scoreSkills`
 * later without changing `matchSkills`.
 */
export function tokenize(text: string): string[] {
  return text.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 1);
}

export function scoreSkills(registry: SkillRegistry, input: string): { skill: Skill; score: number }[] {
  const skills = registry.list();
  const docs = skills.map((s) => tokenize(`${s.name} ${s.description}`));
  const query = tokenize(input);
  const df = new Map<string, number>();
  for (const doc of docs) {
    for (const term of new Set(doc)) df.set(term, (df.get(term) ?? 0) + 1);
  }
  const idf = (term: string): number => Math.log((docs.length + 1) / ((df.get(term) ?? 0) + 1)) + 1;
  const qtf = new Map<string, number>();
  for (const t of query) qtf.set(t, (qtf.get(t) ?? 0) + 1);
  const qnorm = Math.sqrt([...qtf.values()].reduce((n, c) => n + c * c, 0)) || 1;

  return skills.map((skill, i) => {
    // Word-boundary match: `$test` must not fire on `$testing`, nor `$art` on
    // `$artillery` (bare substring includes did both).
    if (new RegExp(`\\$${skill.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\w-])`).test(input)) {
      return { skill, score: Number.POSITIVE_INFINITY };
    }
    const dtf = new Map<string, number>();
    for (const t of docs[i]) dtf.set(t, (dtf.get(t) ?? 0) + 1);
    let dot = 0;
    let dnorm = 0;
    for (const [term, count] of dtf) {
      const w = count * idf(term);
      dnorm += w * w;
      if (qtf.has(term)) dot += w * (qtf.get(term) ?? 0) * idf(term);
    }
    const denom = Math.sqrt(dnorm) * qnorm || 1;
    return { skill, score: dot / denom };
  });
}

export function matchSkills(registry: SkillRegistry, input: string, threshold = 0.05): Skill[] {
  return scoreSkills(registry, input)
    .filter(({ score }) => score >= threshold)
    .sort((a, b) => b.score - a.score)
    .map(({ skill }) => skill);
}
