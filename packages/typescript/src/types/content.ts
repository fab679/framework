import { z } from "zod";

/**
 * Content blocks — standard, provider-neutral message payload
 * Standard message payload: text, reasoning, multimodal and tool-call blocks.
 * `content` stays `string | ContentBlock[]`: strings for simple cases,
 * blocks for multimodal / reasoning / tool-call payloads.
 */

export const TextBlockSchema = z.object({ type: z.literal("text"), text: z.string() });
export const ReasoningBlockSchema = z.object({ type: z.literal("reasoning"), reasoning: z.string() });
export const ImageBlockSchema = z.object({
  type: z.literal("image"),
  url: z.string().optional(),
  data: z.string().optional(),
  mimeType: z.string().optional(),
  fileId: z.string().optional(),
});
export const AudioBlockSchema = z.object({
  type: z.literal("audio"),
  url: z.string().optional(),
  data: z.string().optional(),
  mimeType: z.string().optional(),
  fileId: z.string().optional(),
});
export const FileBlockSchema = z.object({
  type: z.literal("file"),
  url: z.string().optional(),
  data: z.string().optional(),
  mimeType: z.string().optional(),
  fileId: z.string().optional(),
});
export const ToolCallBlockSchema = z.object({
  type: z.literal("tool_call"),
  id: z.string(),
  name: z.string(),
  args: z.unknown(),
});
export const NonStandardBlockSchema = z.object({ type: z.literal("non_standard"), value: z.unknown() });

export const ContentBlockSchema = z.discriminatedUnion("type", [
  TextBlockSchema,
  ReasoningBlockSchema,
  ImageBlockSchema,
  AudioBlockSchema,
  FileBlockSchema,
  ToolCallBlockSchema,
  NonStandardBlockSchema,
]);

export type ContentBlock = z.infer<typeof ContentBlockSchema>;
export type TextBlock = z.infer<typeof TextBlockSchema>;
export type ReasoningBlock = z.infer<typeof ReasoningBlockSchema>;
export type ImageBlock = z.infer<typeof ImageBlockSchema>;

/** Plain-text view of any content shape — what the model "reads". */
export function textOf(content: string | ContentBlock[]): string {
  if (typeof content === "string") return content;
  return content
    .map((b) => {
      switch (b.type) {
        case "text":
          return b.text;
        case "reasoning":
          return b.reasoning;
        case "tool_call":
          return "";
        case "non_standard":
          return "";
        default:
          return "";
      }
    })
    .join("");
}

/**
 * Text for re-sending history to a provider: reasoning blocks are dropped —
 * a model's own past thinking must not come back as ordinary assistant text
 * (duplicated context, and Anthropic rejects thinking-as-text turns).
 */
export function wireTextOf(content: string | ContentBlock[]): string {
  if (typeof content === "string") return content;
  return content
    .filter((b) => b.type !== "reasoning")
    .map((b) => (b.type === "text" ? b.text : ""))
    .join("");
}

export function hasImages(content: string | ContentBlock[]): boolean {
  return typeof content !== "string" && content.some((b) => b.type === "image");
}

/** Images in wire-ready form (url or base64 data). */
export function imagesOf(content: string | ContentBlock[]): { url?: string; data?: string; mimeType?: string }[] {
  if (typeof content === "string") return [];
  return content.filter((b) => b.type === "image").map((b) => ({ url: b.url, data: b.data, mimeType: b.mimeType }));
}
