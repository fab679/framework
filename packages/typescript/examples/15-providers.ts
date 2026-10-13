import { Agent, initChatModel } from "../src/index.js";

// Offline demo: provider resolution for all four backends (no network calls).
// - together/openai/anthropic need keys in .env (see .env.example)
// - ollama needs a local server (http://localhost:11434)
const specs = ["together:Qwen/Qwen3.5-9B", "openai:gpt-4o-mini", "anthropic:claude-sonnet-4-6", "ollama:qwen2.5"];
for (const spec of specs) {
  try {
    const provider = initChatModel(spec, { apiKey: "demo-key" });
    console.log(`${spec} -> ${provider.id}`);
  } catch (err) {
    console.log(`${spec} -> ${(err as Error).message}`);
  }
}

// Local-first: an Ollama-backed agent is built exactly like any other.
const local = new Agent({ name: "local", model: initChatModel("ollama:qwen2.5") });
console.log("agent model:", local.model.id);
