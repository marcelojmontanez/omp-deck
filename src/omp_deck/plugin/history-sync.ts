import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const MAX_HISTORY_MESSAGES = 500;

// Deck displays conversation content, not provider/tool internals. Project before
// framing: a large tool result or argument must not poison history or activity.
export function projectMessage(value: unknown): unknown {
  if (!value || typeof value !== "object") return value;
  const source = value as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const key of ["id", "role", "timestamp", "stopReason", "errorMessage"]) {
    if (key in source) result[key] = source[key];
  }
  const content = source.content;
  result.content = [];
  if (source.role !== "user" && source.role !== "assistant") return result;
  if (typeof content === "string") result.content = content;
  else if (Array.isArray(content)) result.content = content.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    if (item.type === "text" && typeof item.text === "string") return [{ type: "text", text: item.text }];
    if (item.type !== "image") return [];
    // Only small references can be served by the Python blob boundary. Inline
    // data stays in OMP; the image record survives as an unavailable placeholder.
    const image: Record<string, unknown> = { type: "image" };
    if (typeof item.data === "string" && item.data.length <= 128) image.data = item.data;
    if (typeof item.url === "string" && item.url.length <= 256) image.url = item.url;
    return [image];
  });
  return result;
}

export function captureHistory(context: ExtensionContext): readonly unknown[] {
  const branch = context.sessionManager.getBranch();
  const messages: unknown[] = [];
  for (let index = branch.length - 1; index >= 0 && messages.length < MAX_HISTORY_MESSAGES; index--) {
    const entry = branch[index];
    if (entry.type === "message") messages.push(projectMessage(entry.message));
  }
  return messages.reverse();
}

const MAX_HISTORY_CHUNK_MESSAGES = 64;

export async function streamHistory(
  messages: readonly unknown[],
  syncId: string,
  disconnected: () => boolean,
  fits: (data: unknown) => boolean,
  emit: (data: unknown) => Promise<boolean>,
): Promise<boolean> {
  if (messages.length === 0) {
    return emit({ syncId, offset: 0, messages: [], complete: true });
  }
  let offset = 0;
  while (offset < messages.length && !disconnected()) {
    let chunk: unknown[] = [];
    while (offset + chunk.length < messages.length && chunk.length < MAX_HISTORY_CHUNK_MESSAGES) {
      const candidate = [...chunk, messages[offset + chunk.length]];
      const complete = offset + candidate.length === messages.length;
      if (!fits({ syncId, offset, messages: candidate, complete })) break;
      chunk = candidate;
    }
    if (chunk.length === 0) return false;
    const complete = offset + chunk.length === messages.length;
    if (!await emit({ syncId, offset, messages: chunk, complete })) return false;
    offset += chunk.length;
  }
  return !disconnected();
}
