import { timingSafeEqual } from "node:crypto";
import type * as net from "node:net";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const PROTOCOL = "omp-deck-fallback-v1";
const MAX_HANDSHAKE_BYTES = 1024;
const HANDSHAKE_TIMEOUT_MS = 5_000;
type ManagedTimer = Parameters<ExtensionContext["clearTimer"]>[0];

export function authenticateNamedPipe(
  socket: net.Socket,
  context: ExtensionContext,
  token: string,
  authenticated: (remainder: Buffer) => void,
): void {
  let input: Buffer = Buffer.alloc(0);
  let timer: ManagedTimer | undefined;
  const cleanup = (): void => {
    if (timer !== undefined) context.clearTimer(timer);
    timer = undefined;
    socket.off("data", authenticate);
    socket.off("error", cleanup);
    socket.off("close", cleanup);
  };
  const reject = (): void => {
    cleanup();
    socket.destroy();
  };
  const authenticate = (chunk: Buffer): void => {
    if (input.length + chunk.length > MAX_HANDSHAKE_BYTES) {
      reject();
      return;
    }
    input = input.length === 0 ? chunk : Buffer.concat([input, chunk], input.length + chunk.length);
    const newline = input.indexOf(10);
    if (newline < 0) return;
    const remainder = input.subarray(newline + 1);
    try {
      const value: unknown = JSON.parse(input.subarray(0, newline).toString("utf8"));
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid");
      const handshake = value as Record<string, unknown>;
      if (
        Object.keys(handshake).length !== 4
        || handshake.protocol !== PROTOCOL
        || handshake.version !== 1
        || handshake.type !== "connect"
        || typeof handshake.token !== "string"
      ) throw new Error("invalid");
      const supplied = Buffer.from(handshake.token, "utf8");
      const expected = Buffer.from(token, "utf8");
      if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) throw new Error("invalid");
    } catch {
      reject();
      return;
    }
    cleanup();
    authenticated(remainder);
  };
  socket.on("data", authenticate);
  socket.on("error", cleanup);
  socket.on("close", cleanup);
  timer = context.setTimeout(reject, HANDSHAKE_TIMEOUT_MS);
}
