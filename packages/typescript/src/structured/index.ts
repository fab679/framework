import { z } from "zod";
import { zodToJsonSchema } from "zod-to-json-schema";
import { AgentError } from "../types/index.js";

/**
 * Structured output — typed agent results instead of parsed prose.
 * Two strategies:
 * - provider: native `response_format` (OpenAI-compatible APIs). Most reliable.
 * - tool: synthetic `__structured_response` tool; validation errors return as
 *   tool messages so the model self-repairs within maxSteps.
 */

export type ResponseStrategy = "auto" | "provider" | "tool";

export interface ResponseFormat<T = unknown> {
  schema: z.ZodType<T>;
  /** Function/tool name. Default: "structured_response". */
  name?: string;
  strategy?: ResponseStrategy;
  /** Tool message content on success (tool strategy). */
  toolMessageContent?: string;
  /** Validation retries before STRUCTURED_NO_MATCH (tool strategy). Default 2. */
  maxAttempts?: number;
}

export function toolStrategy<T>(schema: z.ZodType<T>, opts?: Omit<ResponseFormat<T>, "schema" | "strategy">): ResponseFormat<T> {
  return { ...opts, schema, strategy: "tool" };
}

export function providerStrategy<T>(schema: z.ZodType<T>, opts?: Omit<ResponseFormat<T>, "schema" | "strategy">): ResponseFormat<T> {
  return { ...opts, schema, strategy: "provider" };
}

export function responseFormatName<T>(format: ResponseFormat<T>): string {
  return format.name ?? "structured_response";
}

export function responseFormatJsonSchema<T>(format: ResponseFormat<T>): Record<string, unknown> {
  return zodToJsonSchema(format.schema, { $refStrategy: "none" }) as Record<string, unknown>;
}

/** Validate raw model output; throws AgentError STRUCTURED_INVALID (retryable) on mismatch. */
export function parseStructured<T>(format: ResponseFormat<T>, raw: unknown): T {
  const parsed = format.schema.safeParse(raw);
  if (parsed.success) return parsed.data;
  throw new AgentError(
    "STRUCTURED_INVALID",
    `Structured output failed validation for "${responseFormatName(format)}": ${parsed.error.message}`,
    true,
  );
}
