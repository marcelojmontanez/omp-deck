import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const MAX_HISTORY_MESSAGES = 500;

export function captureHistory(context: ExtensionContext): readonly unknown[] {
  const branch = context.sessionManager.getBranch();
  const messages: unknown[] = [];
  for (let index = branch.length - 1; index >= 0 && messages.length < MAX_HISTORY_MESSAGES; index--) {
    const entry = branch[index];
    if (entry.type === "message") messages.push(entry.message);
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
