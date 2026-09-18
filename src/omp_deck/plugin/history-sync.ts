import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { normalizeInlineImage } from "./image-content.ts";

const MAX_HISTORY_MESSAGES = 500;
const BLOB_REFERENCE = /^blob:sha256:[a-f0-9]{64}$/;

type ProjectionWarning = (message: string, details: Record<string, unknown>) => void;

interface HistoryEntry {
  type: string;
  message?: unknown;
}

export function createProjectionQueue(generation: () => number, overflow: () => void) {
  let tail = Promise.resolve();
  let queued = 0;
  return function serialize<T>(operation: (current: () => boolean) => Promise<T>): Promise<T> {
    if (queued >= 256) {
      overflow();
      const rejected = Promise.reject<T>(new Error("image projection queue is full"));
      void rejected.catch(() => {});
      return rejected;
    }
    queued++;
    const epoch = generation();
    const pending = tail.then(() => operation(() => epoch === generation())).finally(() => { queued--; });
    tail = pending.then(() => undefined, () => undefined);
    return pending;
  };
}

// Deck displays conversation content, not provider/tool internals. Project before
// framing: a large tool result or argument must not poison history or activity.
export async function projectMessage(
  value: unknown,
  context: ExtensionContext,
  warn: ProjectionWarning,
  current: () => boolean = () => true,
): Promise<unknown> {
  if (!value || typeof value !== "object") return value;
  const source = value as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const key of ["id", "role", "timestamp", "stopReason", "errorMessage"]) {
    if (key in source) result[key] = source[key];
  }
  const content = source.content;
  result.content = [];
  if (source.role !== "user" && source.role !== "assistant") return result;
  if (typeof content === "string") {
    result.content = content;
    return result;
  }
  if (!Array.isArray(content)) return result;
  const projected: unknown[] = [];
  for (let index = 0; index < content.length; index++) {
    const item = content[index];
    if (!item || typeof item !== "object") continue;
    if (item.type === "text" && typeof item.text === "string") {
      projected.push({ type: "text", text: item.text });
      continue;
    }
    if (item.type !== "image") continue;
    if (typeof item.data === "string" && BLOB_REFERENCE.test(item.data)) {
      projected.push({ type: "image", data: item.data });
      continue;
    }
    if (typeof item.data === "string") {
      if (!current()) throw new Error("image projection cancelled");
      try {
        const image = normalizeInlineImage(item);
        const blob = await context.sessionManager.putBlob(image.data);
        if (!BLOB_REFERENCE.test(blob.ref)) throw new Error("blob store returned an invalid reference");
        projected.push({ type: "image", data: blob.ref });
        continue;
      } catch (error) {
        warn("OMP Deck could not preserve an inline image", {
          role: source.role,
          contentIndex: index,
          errorType: error instanceof Error ? error.name : "Error",
        });
      }
    }
    const image: Record<string, unknown> = { type: "image" };
    if (typeof item.url === "string" && item.url.length <= 256) image.url = item.url;
    projected.push(image);
  }
  result.content = projected;
  return result;
}

export function projectTodoResult(toolName: string, result: unknown): unknown[] | undefined {
  if (toolName !== "todo" || !result || typeof result !== "object" || !("details" in result)) return;
  const details = result.details;
  if (!details || typeof details !== "object" || !("phases" in details)) return;
  const phases = details.phases;
  if (Array.isArray(phases) && phases.every(phase => phase && typeof phase === "object" && !Array.isArray(phase))) return phases;
}

export async function captureHistory(
  context: ExtensionContext,
  warn: ProjectionWarning,
  branch: readonly HistoryEntry[] = context.sessionManager.getBranch(),
  current: () => boolean = () => true,
): Promise<readonly unknown[]> {
  const messages: unknown[] = [];
  const selected: unknown[] = [];
  for (let index = branch.length - 1; index >= 0 && selected.length < MAX_HISTORY_MESSAGES; index--) {
    const entry = branch[index];
    if (entry.type === "message" && entry.message !== undefined) selected.push(entry.message);
  }
  for (let index = selected.length - 1; index >= 0; index--) {
    if (!current()) throw new Error("history projection cancelled");
    messages.push(await projectMessage(selected[index], context, warn, current));
  }
  return messages;
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
