import { AgentError } from "../types/index.js";

/**
 * Model capability registry — researched defaults live here so users never
 * pick tiers. Full per-model research (vision, tools, reasoning, context
 * limits) lands later; until then: explicit allowlist + fail-fast error.
 */

export type ModelCapability = "vision" | "tools" | "reasoning-stream";

/** Model ids known to accept image inputs (researched 2026-10; extended as models are verified). */
const VISION_CAPABLE_PATTERNS: RegExp[] = [
  /qwen2.*vl/i,
  /qwen.*vl/i,
  /llama-3\.2-11b-vision/i,
  /llama-3\.2-90b-vision/i,
  /llava/i,
  /minicpm-v/i,
  /gpt-4o/i,
  /gpt-4\.1/i,
  /claude.*sonnet/i,
  /claude.*opus/i,
  /gemini/i,
];

export function supportsVision(modelId: string): boolean {
  return VISION_CAPABLE_PATTERNS.some((re) => re.test(modelId));
}

/** Throw AgentError(MODEL_CAPABILITY) when input needs vision the model lacks. */
export function assertCanHandle(modelId: string, needsVision: boolean): void {
  if (needsVision && !supportsVision(modelId)) {
    throw new AgentError(
      "MODEL_CAPABILITY",
      `Model "${modelId}" cannot take image input. Switch TOGETHER_MODEL to a vision-capable model (e.g. Qwen/Qwen2.5-VL-72B-Instruct) or drop the images.`,
      false,
    );
  }
}
