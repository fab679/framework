import { readFileSync } from "node:fs";
import { Agent } from "../agent/index.js";
import type { TokenUsage } from "../types/index.js";

/**
 * Evals: JSONL datasets + scoring + pass-rate reporter.
 * Offline by default (stub models); set EVAL_LIVE=1 to run cases
 * against the configured provider instead.
 */

export interface EvalExpect {
  contains?: string[];
  toolUsed?: string[];
  noStructuredError?: boolean;
}

export interface EvalCase {
  id: string;
  input: string;
  expect?: EvalExpect;
}

export interface EvalScore {
  id: string;
  pass: boolean;
  detail: string;
  usage: TokenUsage;
}

export function loadJsonl(path: string): EvalCase[] {
  return readFileSync(path, "utf8")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => JSON.parse(l) as EvalCase);
}

export async function runEval(cases: EvalCase[], makeAgent: (c: EvalCase) => Agent): Promise<{ scores: EvalScore[]; passRate: number }> {
  const scores: EvalScore[] = [];
  for (const c of cases) {
    try {
      const res = await makeAgent(c).run(c.input);
      const missing = (c.expect?.contains ?? []).filter((s) => !res.text.includes(s));
      const missingTools = (c.expect?.toolUsed ?? []).filter((t) => !res.toolCalls.some((tc) => tc.name === t));
      const pass = !missing.length && !missingTools.length;
      scores.push({
        id: c.id,
        pass,
        detail: pass ? "ok" : `missing text ${JSON.stringify(missing)} tools ${JSON.stringify(missingTools)} :: ${res.text.slice(0, 120)}`,
        usage: res.usage,
      });
    } catch (err) {
      scores.push({
        id: c.id,
        pass: false,
        detail: `threw: ${err instanceof Error ? err.message : String(err)}`,
        usage: { inputTokens: 0, outputTokens: 0 },
      });
    }
  }
  const passRate = scores.length ? scores.filter((s) => s.pass).length / scores.length : 1;
  return { scores, passRate };
}

export function printReport(scores: EvalScore[], passRate: number): void {
  for (const s of scores) {
    console.log(`${s.pass ? "PASS" : "FAIL"}  ${s.id}  ${s.detail}`);
  }
  console.log(`\npass rate: ${(passRate * 100).toFixed(1)}% (${scores.filter((s) => s.pass).length}/${scores.length})`);
}
