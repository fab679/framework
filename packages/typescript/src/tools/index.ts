import { z } from "zod";
import { AgentError, type ContentBlock } from "../types/index.js";
import type { ToolContext } from "../state/index.js";

export interface ToolResult {
  /** Model-visible content (string or blocks). */
  content: string | ContentBlock[];
  /** Downstream-only data — stored on the call record, never sent to the model. */
  artifact?: unknown;
}

export interface Tool<TArgs = any, TOutput = any> {
  name: string;
  description: string;
  schema: z.ZodType<TArgs, z.ZodTypeDef, any>;
  /** Optional second arg receives ToolContext (state + invocationState). */
  execute: (args: TArgs, ctx?: ToolContext) => Promise<TOutput>;
  /**
   * Split raw output into model-visible content + downstream artifact.
   * Default: JSON/string content, no artifact. Override to keep raw payloads
   * (retrieval metadata, debug blobs) out of the model's context.
   */
  toResult?: (output: TOutput) => ToolResult;
}

/**
 * The defineTool() input: TSchema drives inference — `execute` receives the
 * schema's INPUT type (the agent loop passes the model's RAW args; parse
 * inside `execute` when you need strict validation — no manual generic
 * annotations needed):
 *
 *   defineTool({ schema: z.object({ city: z.string() }),
 *                execute: async ({ city }) => ... })   // city: string
 */
export interface ToolSpec<TSchema extends z.ZodTypeAny = z.ZodTypeAny, TOutput = unknown> {
  name: string;
  description: string;
  schema: TSchema;
  execute: (args: z.input<TSchema>, ctx?: ToolContext) => Promise<TOutput>;
  toResult?: (output: TOutput) => ToolResult;
}

export function defaultToResult(output: unknown): ToolResult {
  return { content: typeof output === "string" ? output : JSON.stringify(output) };
}

export function defineTool<TSchema extends z.ZodTypeAny = z.ZodTypeAny, TOutput = unknown>(
  tool: ToolSpec<TSchema, TOutput>,
): Tool<z.output<TSchema>, TOutput> {
  if (!tool.name) throw new Error("Tool must have a name");
  return { ...tool } as Tool<z.output<TSchema>, TOutput>;
}

/** Validating registry — single place tools are looked up + arg-checked. */
export class ToolRegistry {
  private tools = new Map<string, Tool>();

  register(tool: Tool): this {
    if (this.tools.has(tool.name)) throw new Error(`Tool already registered: ${tool.name}`);
    this.tools.set(tool.name, tool);
    return this;
  }

  get(name: string): Tool | undefined {
    return this.tools.get(name);
  }

  list(): Tool[] {
    return [...this.tools.values()];
  }

  async call<T>(name: string, rawArgs: unknown): Promise<T> {
    const tool = this.tools.get(name);
    if (!tool) throw new AgentError("UNKNOWN_TOOL", `Unknown tool: ${name}`, false);
    let args: unknown;
    try {
      args = tool.schema.parse(rawArgs);
    } catch (err) {
      throw new AgentError(
        "TOOL_ARG_INVALID",
        `Invalid args for tool "${name}": ${err instanceof Error ? err.message : String(err)}`,
        false,
      );
    }
    return (await tool.execute(args)) as T;
  }
}
