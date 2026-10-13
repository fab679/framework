import type { ModelCompletion } from "../../src/models/index.js";

/** Scripted stub model: returns texts in order. */
export function scripted(texts: string[]) {
  let n = 0;
  return {
    id: "scripted",
    complete: async (): Promise<ModelCompletion> => ({
      text: texts[Math.min(n++, texts.length - 1)],
      toolCalls: [],
      usage: { inputTokens: 1, outputTokens: 1 },
    }),
  };
}

/** Single-reply stub model. */
export function textReply(text: string) {
  return scripted([text]);
}
