import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import {
  Agent,
  FileCheckpointer,
  HitlInterrupt,
  defineTool,
  hitlKey,
  humanInTheLoopMiddleware,
} from "../src/index.js";

// Offline demo: suspend on approval, decide as a human, resume to completion.
const dir = mkdtempSync(join(tmpdir(), "hitl-demo-"));
const checkpointer = new FileCheckpointer(dir);

const mkAgent = () =>
  new Agent({
    model: {
      id: "stub",
      complete: async () => ({
        text: "",
        toolCalls: [{ id: "c1", name: "deploy", args: { env: "prod" } }],
        usage: { inputTokens: 1, outputTokens: 1 },
      }),
    },
    maxSteps: 1,
    tools: [
      defineTool({
        name: "deploy",
        description: "Deploys",
        schema: z.object({ env: z.string() }),
        execute: async ({ env }: { env: string }) => `deployed to ${env}`,
      }),
    ],
    middleware: [humanInTheLoopMiddleware({ interruptOn: { deploy: true } })],
  });

try {
  await mkAgent().run("ship it", { checkpointer, threadId: "release-1" });
  console.log("unexpected: no interrupt");
} catch (err) {
  if (!(err instanceof HitlInterrupt)) throw err;
  console.log(`INTERRUPT: ${err.tool}`, err.args);
  // ...human reviews, then approves:
  const res = await mkAgent().resume("release-1", { [hitlKey("deploy", { env: "prod" })]: "approve" }, { checkpointer });
  console.log("RESUMED:", res.toolCalls[0]?.output);
}
