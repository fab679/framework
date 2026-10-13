export async function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  signal?.throwIfAborted();
  if (ms <= 0) return;
  await new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

/**
 * Local token estimate — dependency-free approximation, not a BPE.
 * Counts word-ish runs, numbers, CJK characters, and punctuation clusters
 * separately (close to subword behavior on prose; ±30% on code).
 * Calibrated so `estimateTokens("abcd") === 1`. Prefer provider usage
 * metadata when available; use this for budgets and triggers only.
 */
export function estimateTokens(text: string): number {
  if (!text) return 0;
  const words = text.match(/[A-Za-z]+(?:'[A-Za-z]+)?|\d+(?:\.\d+)?|[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]|[^A-Za-z\d\s]/g);
  if (!words) return 0;
  let tokens = 0;
  for (const w of words) {
    if (/^[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]$/.test(w)) {
      tokens += 1;
    } else if (/^[A-Za-z]+(?:'[A-Za-z]+)?$/.test(w)) {
      tokens += Math.max(1, Math.ceil(w.length / 5));
    } else {
      tokens += 1;
    }
  }
  return tokens;
}

/** Rough USD cost for usage against a per-1M-token price table. */
export function estimateCostUsd(
  usage: { inputTokens: number; outputTokens: number },
  price: { inputPerMillion: number; outputPerMillion: number },
): number {
  return (usage.inputTokens * price.inputPerMillion + usage.outputTokens * price.outputPerMillion) / 1_000_000;
}
