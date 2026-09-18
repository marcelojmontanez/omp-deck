import { createHash } from "node:crypto";
import type * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { BoundedEventQueue } from "./bounded-event-queue.ts";
import type { BoundedLineReader } from "./bounded-line-reader.ts";
import type { BoundedRequestDispatcher } from "./bounded-request-dispatcher.ts";
import type { BoundedSocketWriter } from "./bounded-writer.ts";
import { readThinkingState } from "./thinking-state.ts";

export const PROTOCOL = "omp-deck-fallback-v1";
export const MAX_LINE_BYTES = 1024 * 1024;
export const WINDOWS = process.platform === "win32";
export const UNIX_SOCKET_PATH_BYTES = process.platform === "linux" ? 108 : 104;
export function socketDirectory(): string {
  return WINDOWS
    ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "OMP Deck", "state", "sockets")
    : path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"), "omp-deck", "sockets");
}

export type JsonObject = Record<string, unknown>;
export interface Peer {
  active: boolean;
  readonly context: ExtensionContext;
  readonly generation: number;
  readonly sessionId: string;
  socket: net.Socket;
  input: BoundedLineReader;
  lastPong: number;
  requests: BoundedRequestDispatcher<JsonObject>;
  sequence: number;
  writer: BoundedSocketWriter;
  events: BoundedEventQueue;
}

export function sessionSocketId(sessionId: string): string {
  return createHash("sha256").update(sessionId).digest("hex").slice(0, 24);
}

export function unixEndpointName(sessionId: string, connectionId: string): string {
  return createHash("sha256")
    .update(sessionId).update("\0").update(connectionId).digest("hex").slice(0, 24) + ".sock";
}

export function encoded(payload: JsonObject): string | undefined {
  try {
    const line = `${JSON.stringify(payload)}\n`;
    return Buffer.byteLength(line) <= MAX_LINE_BYTES ? line : undefined;
  } catch {
    return undefined;
  }
}

export function eventLine(peer: Peer, kind: string, data: unknown): string | undefined {
  return encoded({
    protocol: PROTOCOL,
    type: "event",
    sequence: peer.sequence + 1,
    kind,
    data,
  });
}

export function snapshots(pi: ExtensionAPI, context: ExtensionContext): JsonObject {
  return {
    idle: context.isIdle(),
    pendingMessages: context.hasPendingMessages(),
    model: context.model ? { provider: context.model.provider, id: context.model.id } : null,
    async: context.getAsyncJobSnapshot(),
    ...readThinkingState(pi, context),
  };
}
