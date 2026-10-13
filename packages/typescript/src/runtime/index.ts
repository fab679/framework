import { AgentError } from "../types/index.js";

export interface Budget {
  signal?: AbortSignal;
  timeoutMs?: number;
}

function toAbortError(): AgentError {
  return new AgentError("ABORTED", "Run aborted", false);
}

/** Runtime guard: abort + timeout. Token/cost budgets land here in P1. */
export async function runWithBudget<T>(fn: () => Promise<T>, budget: Budget = {}): Promise<T> {
  const { signal, timeoutMs } = budget;
  try {
    signal?.throwIfAborted();
  } catch {
    throw toAbortError();
  }

  if (!timeoutMs && !signal) return fn();

  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      fn(),
      new Promise<never>((_, reject) => {
        if (timeoutMs) {
          timer = setTimeout(() => reject(new AgentError("TIMEOUT", `Exceeded ${timeoutMs}ms`, true)), timeoutMs);
        }
        signal?.addEventListener(
          "abort",
          () => {
            if (timer) clearTimeout(timer);
            reject(toAbortError());
          },
          { once: true },
        );
      }),
    ]);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw toAbortError();
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
}
