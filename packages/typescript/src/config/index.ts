import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { config } from "dotenv";
import { z } from "zod";

// Load .env from cwd, then walk up (handles running from the package dir
// inside the monorepo). FRAMEWORK_ENV_PATH overrides everything.
// Each step is silent when the file is missing — never throws, never logs keys.
function loadEnv(): void {
  if (process.env.FRAMEWORK_ENV_PATH) {
    config({ path: process.env.FRAMEWORK_ENV_PATH });
    return;
  }
  config();
  let dir = resolve(process.cwd());
  for (let i = 0; i < 3; i++) {
    dir = dirname(dir);
    const candidate = join(dir, ".env");
    if (existsSync(candidate)) {
      config({ path: candidate });
      return;
    }
  }
}

loadEnv();

const ConfigSchema = z.object({
  TOGETHER_API_KEY: z.string().default(""),
  TOGETHER_BASE_URL: z.string().default("https://api.together.xyz/v1"),
  /** Small chat default — one model, no tiers. */
  TOGETHER_MODEL: z.string().default("Qwen/Qwen3.5-9B"),
  OPENAI_BASE_URL: z.string().default("https://api.openai.com/v1"),
  OPENAI_API_KEY: z.string().default(""),
  OPENAI_MODEL: z.string().default("gpt-4o-mini"),
  ANTHROPIC_API_KEY: z.string().default(""),
  ANTHROPIC_BASE_URL: z.string().default("https://api.anthropic.com"),
  ANTHROPIC_MODEL: z.string().default("claude-sonnet-4-6"),
  OLLAMA_BASE_URL: z.string().default("http://localhost:11434/v1"),
  OLLAMA_MODEL: z.string().default("qwen2.5"),
  E2E_MODEL: z.string().default(""),
});

export type FrameworkConfig = z.infer<typeof ConfigSchema> & {
  /** Resolved provider settings: TogetherAI wins when its key is set. */
  active: { baseUrl: string; apiKey: string; model: string; source: "together" | "openai" };
};

let cached: FrameworkConfig | undefined;

export function loadConfig(env: NodeJS.ProcessEnv = process.env): FrameworkConfig {
  if (cached) return cached;
  const parsed = ConfigSchema.parse({
    TOGETHER_API_KEY: env.TOGETHER_API_KEY ?? "",
    TOGETHER_BASE_URL: env.TOGETHER_BASE_URL,
    TOGETHER_MODEL: env.TOGETHER_MODEL,
    OPENAI_BASE_URL: env.OPENAI_BASE_URL,
    OPENAI_API_KEY: env.OPENAI_API_KEY ?? "",
    OPENAI_MODEL: env.OPENAI_MODEL,
    ANTHROPIC_API_KEY: env.ANTHROPIC_API_KEY ?? "",
    ANTHROPIC_BASE_URL: env.ANTHROPIC_BASE_URL,
    ANTHROPIC_MODEL: env.ANTHROPIC_MODEL,
    OLLAMA_BASE_URL: env.OLLAMA_BASE_URL,
    OLLAMA_MODEL: env.OLLAMA_MODEL,
    E2E_MODEL: env.E2E_MODEL ?? "",
  });
  const useTogether = parsed.TOGETHER_API_KEY.length > 0;
  const model = useTogether ? parsed.TOGETHER_MODEL : parsed.OPENAI_MODEL;
  cached = {
    ...parsed,
    active: useTogether
      ? { baseUrl: parsed.TOGETHER_BASE_URL, apiKey: parsed.TOGETHER_API_KEY, model, source: "together" }
      : { baseUrl: parsed.OPENAI_BASE_URL, apiKey: parsed.OPENAI_API_KEY, model, source: "openai" },
  };
  return cached;
}

/** Test-only: clear cache so tests can pass custom env objects. */
export function __resetConfig(): void {
  cached = undefined;
}
