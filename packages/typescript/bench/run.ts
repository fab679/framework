import { writeFileSync } from "node:fs";
import { Agent } from "../src/index.js";
import type { ModelCompletion } from "../src/models/index.js";

/**
 * Token/latency micro-bench (offline stubs — measures harness overhead,
 * not providers). Writes bench/results.json; CI fails on regression
 * when --baseline is given and p50 latency grows >20%.
 *   pnpm --filter framework-agents bench [-- --baseline]
 */

const stub = (delayMs: number) => ({
  id: "bench",
  complete: async (): Promise<ModelCompletion> => {
    if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
    return { text: "ok", toolCalls: [], usage: { inputTokens: 100, outputTokens: 50 } };
  },
});

interface BenchRow {
  name: string;
  runs: number;
  p50Ms: number;
  tokensPerRun: number;
}

async function bench(name: string, runs: number, make: () => Agent): Promise<BenchRow> {
  const lat: number[] = [];
  let tokens = 0;
  for (let i = 0; i < runs; i++) {
    const t0 = Date.now();
    const res = await make().run("bench");
    lat.push(Date.now() - t0);
    tokens += res.usage.inputTokens + res.usage.outputTokens;
  }
  lat.sort((a, b) => a - b);
  return { name, runs, p50Ms: lat[Math.floor(lat.length / 2)], tokensPerRun: Math.round(tokens / runs) };
}

const rows = [
  await bench("plain-run", 20, () => new Agent({ model: stub(0) })),
  await bench("model-5ms", 10, () => new Agent({ model: stub(5) })),
];

console.log(JSON.stringify(rows, null, 1));
writeFileSync(new URL("./results.json", import.meta.url), `${JSON.stringify({ at: new Date().toISOString(), rows }, null, 1)}\n`);

const baselineArg = process.argv.includes("--baseline");
if (baselineArg) {
  const { readFileSync, existsSync } = await import("node:fs");
  const basePath = new URL("./baseline.json", import.meta.url);
  if (!existsSync(basePath)) {
    writeFileSync(basePath, JSON.stringify(rows, null, 1));
    console.log("baseline written");
  } else {
    const base = JSON.parse(readFileSync(basePath, "utf8")) as BenchRow[];
    let failed = false;
    for (const row of rows) {
      const prev = base.find((b) => b.name === row.name);
      if (prev && row.p50Ms > prev.p50Ms * 1.2 + 5) {
        console.error(`REGRESSION ${row.name}: p50 ${prev.p50Ms}ms -> ${row.p50Ms}ms`);
        failed = true;
      }
    }
    if (failed) process.exit(1);
    console.log("no regressions");
  }
}
