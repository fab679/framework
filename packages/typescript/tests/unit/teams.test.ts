import { describe, expect, it } from "vitest";
import { Agent } from "../../src/agent/index.js";
import { MockModelProvider } from "../../src/models/index.js";
import { runOrchestrated } from "../../src/orchestration/index.js";
import { runWorkflow } from "../../src/workflows/index.js";

function echoAgent(name: string, description = "") {
  return new Agent({
    name,
    description,
    model: {
      id: `echo:${name}`,
      complete: async (opts) => ({
        text: `${name} saw: ${opts.messages.at(-1)?.content}`,
        toolCalls: [],
        usage: { inputTokens: 1, outputTokens: 1 },
      }),
    },
  });
}

describe("orchestration", () => {
  it("sequential chains output into the next agent", async () => {
    const out = await runOrchestrated(
      { strategy: "sequential", agents: [echoAgent("a"), echoAgent("b")] },
      "start",
    );
    expect(out.results).toHaveLength(2);
    expect(out.text).toContain("b saw:");
    expect(out.results[1].text).toContain("a saw: start");
  });

  it("sequential rejects empty teams", async () => {
    await expect(runOrchestrated({ strategy: "sequential", agents: [] }, "x")).rejects.toMatchObject({
      code: "ORCHESTRATION_EMPTY",
    });
  });

  it("parallel keeps agent order with a concurrency cap", async () => {
    const order: string[] = [];
    const slow = (name: string, ms: number) =>
      new Agent({
        name,
        model: {
          id: `slow:${name}`,
          complete: async () => {
            await new Promise((r) => setTimeout(r, ms));
            order.push(name);
            return { text: name, toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
          },
        },
      });
    const out = await runOrchestrated(
      { strategy: "parallel", agents: [slow("a", 30), slow("b", 5), slow("c", 5)], concurrency: 2 },
      "go",
    );
    expect(out.results.map((r) => r.text)).toEqual(["a", "b", "c"]);
    expect(out.text).toBe("a\n\nb\n\nc");
    expect(order).toContain("a");
  });

  it("router picks the right specialist", async () => {
    const routerModel = new MockModelProvider();
    // Mock always says mock-response-N; force index via canned model instead
    const picker = {
      id: "picker",
      complete: async () => ({ text: "1", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
    };
    const out = await runOrchestrated(
      {
        strategy: "router",
        agents: [echoAgent("billing", "invoices and refunds"), echoAgent("shipping", "parcels and tracking")],
        routerModel: picker,
      },
      "where is my parcel?",
    );
    expect(out.routedTo).toBe(1);
    expect(out.text).toContain("shipping saw:");
    expect(routerModel).toBeDefined();
  });

  it("router rejects invalid picks", async () => {
    const picker = {
      id: "picker",
      complete: async () => ({ text: "hello there", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
    };
    await expect(
      runOrchestrated({ strategy: "router", agents: [echoAgent("a")], routerModel: picker }, "x"),
    ).rejects.toMatchObject({ code: "ROUTER_NO_MATCH" });
  });
});

describe("workflows", () => {
  it("runs DAG in dependency order with shared ctx", async () => {
    const seen: string[] = [];
    const ctx = await runWorkflow([
      { id: "fetch", run: async () => { seen.push("fetch"); return [1, 2]; } },
      { id: "double", dependsOn: ["fetch"], run: async (c) => { seen.push("double"); return (c.fetch as number[]).map((n) => n * 2); } },
      { id: "audit", dependsOn: ["fetch"], run: async () => { seen.push("audit"); return "ok"; } },
      { id: "sum", dependsOn: ["double", "audit"], run: async (c) => (c.double as number[]).reduce((a, b) => a + b, 0) },
    ]);
    expect(ctx.sum).toBe(6);
    expect(seen[0]).toBe("fetch");
    expect(seen.slice(1).sort()).toEqual(["audit", "double"]);
  });

  it("rejects duplicates, unknown deps, self-deps, cycles", async () => {
    const run = async () => 1;
    await expect(runWorkflow([{ id: "a", run }, { id: "a", run }])).rejects.toMatchObject({ code: "WORKFLOW_DUPLICATE_ID" });
    await expect(runWorkflow([{ id: "a", dependsOn: ["ghost"], run }])).rejects.toMatchObject({ code: "WORKFLOW_UNKNOWN_DEP" });
    await expect(runWorkflow([{ id: "a", dependsOn: ["a"], run }])).rejects.toMatchObject({ code: "WORKFLOW_CYCLE" });
    await expect(
      runWorkflow([
        { id: "a", dependsOn: ["b"], run },
        { id: "b", dependsOn: ["a"], run },
      ]),
    ).rejects.toMatchObject({ code: "WORKFLOW_CYCLE" });
  });

  it("retries failing nodes then succeeds", async () => {
    let n = 0;
    const ctx = await runWorkflow([
      {
        id: "flaky",
        retry: { attempts: 3, backoffMs: 1 },
        run: async () => { n += 1; if (n < 3) throw new Error("boom"); return "recovered"; },
      },
    ]);
    expect(ctx.flaky).toBe("recovered");
    expect(n).toBe(3);
  });

  it("gives up after attempts", async () => {
    await expect(
      runWorkflow([{ id: "bad", retry: { attempts: 2, backoffMs: 1 }, run: async () => { throw new Error("nope"); } }]),
    ).rejects.toThrow("nope");
  });

  it("honors abort", async () => {
    const c = new AbortController();
    c.abort();
    await expect(runWorkflow([{ id: "a", run: async () => 1 }], { signal: c.signal })).rejects.toThrow();
  });
});
