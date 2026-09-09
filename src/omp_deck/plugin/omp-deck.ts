import { createHash, randomBytes, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { authenticateNamedPipe } from "./named-pipe-auth.ts";

const PROTOCOL = "omp-deck-fallback-v1";
const MAX_LINE_BYTES = 1024 * 1024;
const HEARTBEAT_MS = 15_000;
const MAX_MISSED_HEARTBEATS = 3;
const MAX_QUEUED_REQUESTS = 64;
const MAX_PENDING_WRITE_BYTES = 4 * MAX_LINE_BYTES;
const MAX_HISTORY_MESSAGES = 500;
const MAX_HISTORY_CHUNK_MESSAGES = 64;
const WINDOWS = process.platform === "win32";
const UNIX_SOCKET_PATH_BYTES = process.platform === "linux" ? 108 : 104;
const SOCKET_DIR = WINDOWS
  ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "OMP Deck", "state", "sockets")
  : path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"), "omp-deck", "sockets");

type JsonObject = Record<string, unknown>;
type ManagedTimer = Parameters<ExtensionContext["clearTimer"]>[0];

interface Peer {
  socket: net.Socket;
  input: Buffer;
  lastPong: number;
  queuedRequests: number;
  dispatchTail: Promise<void>;
  sequence: number;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}


export default function ompDeck(pi: ExtensionAPI) {
  const connectionId = randomUUID();
  const peers = new Set<Peer>();
  let ctx: ExtensionContext | undefined;
  const windowsToken = randomBytes(32).toString("hex");
  let server: net.Server | undefined;
  let socketPath: string | undefined;
  let metadataPath: string | undefined;
  let retryTimer: ManagedTimer | undefined;
  let heartbeatTimer: ManagedTimer | undefined;
  let reconnectAttempt = 0;
  const sessionSocketId = (sessionId: string): string => createHash("sha256").update(sessionId).digest("hex").slice(0, 24);
  const unixEndpointName = (sessionId: string): string => createHash("sha256")
    .update(sessionId).update("\0").update(connectionId).digest("hex").slice(0, 24) + ".sock";
  let stopping = false;
  let lifecycleGeneration = 0;
  let lifecycleTail = Promise.resolve();
  let historyGeneration = 0;

  const sessionIdentity = (context: ExtensionContext): { id: string; file: string | null; title: string | null } => ({
    id: context.sessionManager.getSessionId(),
    file: context.sessionManager.getSessionFile() || null,
    title: context.sessionManager.getSessionName() || null,
  });

  const registration = (context: ExtensionContext): JsonObject => {
    const session = sessionIdentity(context);
    return {
      protocol: PROTOCOL,
      version: 1,
      type: "registration",
      ompVersion: pi.pi.VERSION,
      compatible: true,
      connectionId,
      session,
      sessionId: session.id,
      title: session.title,
      project: context.cwd,
      capabilities: {
        sendMessage: true,
        followUp: true,
        interrupt: true,
        shutdown: true,
        todos: true,
        subagents: true,
        answerUiRequest: false,
        answer_ui_request: false,
      },
    };
  };

  function encoded(payload: JsonObject): string | undefined {
    try {
      const line = `${JSON.stringify(payload)}\n`;
      return Buffer.byteLength(line) <= MAX_LINE_BYTES ? line : undefined;
    } catch {
      return undefined;
    }
  }

  function writeLine(peer: Peer, line: string): boolean {
    if (peer.socket.destroyed) return false;
    const bytes = Buffer.byteLength(line);
    if (peer.socket.writableLength + bytes > MAX_PENDING_WRITE_BYTES) {
      peer.socket.destroy(new Error("outbound queue limit exceeded"));
      return false;
    }
    try {
      // Node queues a false-returning write until "drain"; writableLength is
      // bounded above, so producers may continue without unbounded buffering.
      peer.socket.write(line);
      return true;
    } catch (error) {
      pi.logger.warn("OMP Deck fallback write failed", { error: errorText(error) });
      peer.socket.destroy();
      return false;
    }
  }

  function send(peer: Peer, payload: JsonObject): boolean {
    const line = encoded(payload);
    return line !== undefined && writeLine(peer, line);
  }

  function eventLine(peer: Peer, kind: string, data: unknown): string | undefined {
    return encoded({
      protocol: PROTOCOL,
      type: "event",
      sequence: peer.sequence + 1,
      kind,
      data,
    });
  }

  function sendEvent(peer: Peer, kind: string, data: unknown = {}): boolean {
    const sequence = peer.sequence + 1;
    let wireKind = kind;
    let wireData = data;
    let line = eventLine(peer, wireKind, wireData);
    let degraded = false;
    if (!line) {
      degraded = true;
      if (kind === "snapshot") {
        const snapshot = data && typeof data === "object"
          ? data as JsonObject
          : {};
        wireData = {
          idle: snapshot.idle === true,
          pendingMessages: snapshot.pendingMessages === true,
          model: null,
          async: [],
        };
      } else {
        wireKind = "degraded";
        wireData = { droppedKind: kind, reason: "event exceeds wire limit" };
      }
      line = eventLine(peer, wireKind, wireData);
    }
    if (!line || !writeLine(peer, line)) return false;
    peer.sequence = sequence;
    if (degraded && kind === "snapshot") {
      sendEvent(peer, "degraded", {
        droppedKind: kind,
        reason: "snapshot collections exceed wire limit",
      });
    }
    return true;
  }

  function emit(kind: string, data: unknown = {}): void {
    for (const peer of peers) sendEvent(peer, kind, data);
  }

  function activeHistory(context: ExtensionContext): unknown[] {
    const messages: unknown[] = [];
    for (const entry of context.sessionManager.getBranch()) {
      if (entry.type === "message") messages.push(entry.message);
    }
    return messages.slice(-MAX_HISTORY_MESSAGES);
  }

  function sendHistory(peer: Peer, context: ExtensionContext): void {
    const syncId = `${connectionId}:${++historyGeneration}`;
    const messages = activeHistory(context);
    if (messages.length === 0) {
      sendEvent(peer, "history", { syncId, offset: 0, messages: [], complete: true });
      return;
    }
    let offset = 0;
    while (offset < messages.length) {
      let chunk: unknown[] = [];
      while (offset + chunk.length < messages.length && chunk.length < MAX_HISTORY_CHUNK_MESSAGES) {
        const candidate = [...chunk, messages[offset + chunk.length]];
        const complete = offset + candidate.length === messages.length;
        if (!eventLine(peer, "history", { syncId, offset, messages: candidate, complete })) break;
        chunk = candidate;
      }
      if (chunk.length === 0) {
        sendEvent(peer, "degraded", {
          droppedKind: "history",
          reason: "history message exceeds wire limit",
        });
        return;
      }
      const complete = offset + chunk.length === messages.length;
      if (!sendEvent(peer, "history", { syncId, offset, messages: chunk, complete })) return;
      offset += chunk.length;
    }
  }

  function emitHistory(context: ExtensionContext): void {
    for (const peer of peers) sendHistory(peer, context);
  }

  function snapshots(context: ExtensionContext): JsonObject {
    return {
      idle: context.isIdle(),
      pendingMessages: context.hasPendingMessages(),
      model: context.model ? { provider: context.model.provider, id: context.model.id } : null,
      async: context.getAsyncJobSnapshot(),
    };
  }

  async function dispatch(peer: Peer, request: JsonObject): Promise<void> {
    const id = request.id;
    if ((typeof id !== "string" && typeof id !== "number") || request.type !== "request" || request.protocol !== PROTOCOL) {
      send(peer, { protocol: PROTOCOL, type: "response", id: id ?? null, ok: false, error: "invalid request" });
      return;
    }
    const active = ctx;
    if (!active) {
      send(peer, { protocol: PROTOCOL, type: "response", id, ok: false, error: "session unavailable" });
      return;
    }
    try {
      switch (request.action) {
        case "sendMessage":
        case "followUp": {
          if (typeof request.message !== "string" || request.message.length === 0 || Buffer.byteLength(request.message) > MAX_LINE_BYTES / 2) {
            throw new Error("invalid message");
          }
          await pi.sendUserMessage(request.message, request.action === "followUp" ? { deliverAs: "followUp" } : undefined);
          break;
        }
        case "interrupt":
          active.abort();
          break;
        case "shutdown":
          active.shutdown();
          break;
        case "snapshot": {
          const response = {
            protocol: PROTOCOL,
            type: "response",
            id,
            ok: true,
            result: { ...snapshots(active), eventSequence: peer.sequence },
          };
          if (!send(peer, response)) {
            send(peer, {
              protocol: PROTOCOL,
              type: "response",
              id,
              ok: false,
              error: "snapshot exceeds wire limit",
            });
          }
          return;
        }
        case "syncHistory":
          sendHistory(peer, active);
          break;
        case "pong":
          peer.lastPong = Date.now();
          break;
        default:
          throw new Error("unsupported action");
      }
      send(peer, { protocol: PROTOCOL, type: "response", id, ok: true });
    } catch (error) {
      send(peer, { protocol: PROTOCOL, type: "response", id, ok: false, error: errorText(error) });
    }
  }

  function attach(socket: net.Socket, context: ExtensionContext): void {
    const peer: Peer = {
      socket,
      input: Buffer.alloc(0),
      lastPong: Date.now(),
      queuedRequests: 0,
      dispatchTail: Promise.resolve(),
      sequence: 0,
    };
    peers.add(peer);
    if (!send(peer, registration(context))) {
      peers.delete(peer);
      socket.destroy(new Error("registration exceeds wire limit"));
      return;
    }
    sendEvent(peer, "snapshot", snapshots(context));
    socket.on("data", (chunk: Buffer) => {
      if (peer.input.length + chunk.length > MAX_LINE_BYTES) {
        send(peer, { protocol: PROTOCOL, type: "response", id: null, ok: false, error: "line exceeds 1 MiB" });
        socket.destroy();
        return;
      }
      peer.input = peer.input.length === 0 ? chunk : Buffer.concat([peer.input, chunk], peer.input.length + chunk.length);
      let newline = peer.input.indexOf(10);
      while (newline >= 0) {
        const line = peer.input.subarray(0, newline);
        peer.input = peer.input.subarray(newline + 1);
        if (line.length) {
          try {
            const parsed: unknown = JSON.parse(line.toString("utf8"));
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("request must be an object");
            if (peer.queuedRequests >= MAX_QUEUED_REQUESTS) {
              send(peer, { protocol: PROTOCOL, type: "response", id: null, ok: false, error: "request queue limit exceeded" });
              socket.destroy();
              return;
            }
            peer.queuedRequests++;
            peer.dispatchTail = peer.dispatchTail
              .then(() => dispatch(peer, parsed as JsonObject))
              .catch((error) => pi.logger.warn("OMP Deck fallback request failed", { error: errorText(error) }))
              .finally(() => { peer.queuedRequests--; });
          } catch (error) {
            send(peer, { protocol: PROTOCOL, type: "response", id: null, ok: false, error: `invalid JSON: ${errorText(error)}` });
          }
        }
        newline = peer.input.indexOf(10);
      }
    });
    socket.on("error", (error) => pi.logger.warn("OMP Deck fallback peer error", { error: error.message }));
    socket.on("close", () => peers.delete(peer));
  }
  function accept(socket: net.Socket, context: ExtensionContext): void {
    if (!WINDOWS) {
      attach(socket, context);
      return;
    }
    authenticateNamedPipe(socket, context, windowsToken, (remainder) => {
      attach(socket, context);
      if (remainder.length) socket.emit("data", remainder);
    });
  }



  async function removeFiles(): Promise<void> {
    const files = WINDOWS ? [metadataPath] : [metadataPath, socketPath];
    for (const file of files) {
      if (file) await fs.promises.unlink(file).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== "ENOENT") pi.logger.warn("OMP Deck fallback cleanup failed", { error: error.message });
      });
    }
    metadataPath = undefined;
    socketPath = undefined;
  }

  async function closeServer(): Promise<void> {
    const active = server;
    server = undefined;
    for (const peer of peers) peer.socket.destroy();
    peers.clear();
    if (active) await new Promise<void>((resolve) => active.close(() => resolve()));
    await removeFiles();
  }

  function scheduleReconnect(context: ExtensionContext): void {
    if (stopping || retryTimer) return;
    const delay = Math.min(30_000, 250 * (2 ** Math.min(reconnectAttempt++, 7)));
    retryTimer = context.setTimeout(() => {
      retryTimer = undefined;
      queueRestart(context);
    }, delay);
  }

  async function start(context: ExtensionContext, generation: number): Promise<void> {
    if (stopping || generation !== lifecycleGeneration) return;
    let listener: net.Server | undefined;
    try {
      await closeServer();
      if (stopping || generation !== lifecycleGeneration) return;
      await fs.promises.mkdir(SOCKET_DIR, { recursive: true, mode: 0o700 });
      if (!WINDOWS) await fs.promises.chmod(SOCKET_DIR, 0o700);
      if (stopping || generation !== lifecycleGeneration) return;
      const identity = sessionIdentity(context);
      const endpointName = WINDOWS
        ? `${sessionSocketId(identity.id)}.${process.pid}.${connectionId}.pipe`
        : unixEndpointName(identity.id);
      socketPath = WINDOWS ? `\\\\.\\pipe\\${endpointName}` : path.join(SOCKET_DIR, endpointName);
      const socketPathBytes = WINDOWS ? 0 : Buffer.byteLength(socketPath) + 1;
      if (socketPathBytes > UNIX_SOCKET_PATH_BYTES) {
        throw new Error(`Unix socket path requires ${socketPathBytes} bytes; platform limit is ${UNIX_SOCKET_PATH_BYTES}`);
      }
      metadataPath = path.join(SOCKET_DIR, `${endpointName}.json`);
      listener = net.createServer((socket) => accept(socket, context));
      server = listener;
      listener.on("error", (error) => {
        pi.logger.warn("OMP Deck fallback listener error", { error: error.message });
        if (server !== listener) return;
        void closeServer()
          .then(() => scheduleReconnect(context))
          .catch((closeError) => pi.logger.warn("OMP Deck fallback listener cleanup failed", { error: errorText(closeError) }));
      });
      await new Promise<void>((resolve, reject) => {
        listener!.once("error", reject);
        listener!.listen(socketPath, () => { listener!.off("error", reject); resolve(); });
      });
      if (stopping || generation !== lifecycleGeneration || server !== listener) {
        if (server === listener) await closeServer();
        else if (listener.listening) await new Promise<void>((resolve) => listener!.close(() => resolve()));
        return;
      }
      if (!WINDOWS) await fs.promises.chmod(socketPath, 0o600);
      const metadata = WINDOWS
        ? { ...registration(context), endpoint: socketPath, pid: process.pid, authToken: windowsToken }
        : registration(context);
      await fs.promises.writeFile(metadataPath, `${JSON.stringify(metadata)}\n`, { mode: 0o600 });
      if (stopping || generation !== lifecycleGeneration || server !== listener) {
        if (server === listener) await closeServer();
        return;
      }
      reconnectAttempt = 0;
    } catch (error) {
      if (listener && server === listener) await closeServer();
      pi.logger.warn("OMP Deck fallback unavailable", { error: errorText(error) });
      if (generation === lifecycleGeneration) scheduleReconnect(context);
    }
  }

  function queueRestart(context: ExtensionContext): Promise<void> {
    const generation = ++lifecycleGeneration;
    lifecycleTail = lifecycleTail.then(() => start(context, generation)).catch((error) => {
      pi.logger.warn("OMP Deck fallback lifecycle failed", { error: errorText(error) });
    });
    return lifecycleTail;
  }

  function startHeartbeat(context: ExtensionContext): void {
    if (heartbeatTimer) return;
    heartbeatTimer = context.setInterval(() => {
      const now = Date.now();
      for (const peer of peers) {
        if (now - peer.lastPong > HEARTBEAT_MS * MAX_MISSED_HEARTBEATS) peer.socket.destroy();
        else sendEvent(peer, "heartbeat", { time: now });
      }
    }, HEARTBEAT_MS);
  }

  // Handlers are deliberately registered synchronously during factory load.
  pi.on("session_start", async (_event, context) => {
    ctx = context;
    stopping = false;
    for (const peer of peers) peer.sequence = 0;
    await queueRestart(context);
    startHeartbeat(context);
    emit("lifecycle", { state: "session_start", session: sessionIdentity(context), cwd: context.cwd });
  });
  pi.on("session_switch", async (_event, context) => { ctx = context; emit("lifecycle", { state: "session_switch" }); await queueRestart(context); });
  pi.on("session_branch", async (_event, context) => { ctx = context; emit("lifecycle", { state: "session_branch" }); await queueRestart(context); });
  pi.on("session_tree", (_event, context) => { ctx = context; emitHistory(context); });
  pi.on("session_compact", (_event, context) => { ctx = context; emitHistory(context); });
  pi.on("agent_start", (event) => emit("lifecycle", { state: "agent_start", event }));
  pi.on("agent_end", (event, context) => emit("lifecycle", { state: "agent_end", event, snapshot: snapshots(context) }));
  pi.on("message_start", (event) => emit("message", { phase: "start", message: event.message }));
  pi.on("message_update", (event) => emit("message", { phase: "update", message: event.message, update: event.assistantMessageEvent }));
  pi.on("message_end", (event) => emit("message", { phase: "end", message: event.message }));
  pi.on("tool_execution_start", (event) => emit("tool", event.toolName.toLowerCase() === "ask"
    ? { phase: "start", ...event, terminalOnly: true, instruction: "Answer this request in the terminal" }
    : { phase: "start", ...event }));
  pi.on("tool_execution_update", (event) => emit("tool", { phase: "update", ...event }));
  pi.on("tool_execution_end", (event) => emit("tool", { phase: "end", ...event }));
  pi.on("todo_reminder", (event) => emit("todo", { todos: event.todos, attempt: event.attempt }));
  pi.on("goal_updated", (event) => emit("todo", { goal: event.goal, state: event.state }));
  pi.on("turn_end", (event, context) => emit("async", { event, snapshot: context.getAsyncJobSnapshot() }));
  pi.on("session_shutdown", async () => {
    stopping = true;
    lifecycleGeneration++;
    emit("lifecycle", { state: "session_shutdown" });
    if (ctx && retryTimer) ctx.clearTimer(retryTimer);
    if (ctx && heartbeatTimer) ctx.clearTimer(heartbeatTimer);
    retryTimer = undefined;
    heartbeatTimer = undefined;
    lifecycleTail = lifecycleTail.then(() => closeServer()).catch((error) => {
      pi.logger.warn("OMP Deck fallback shutdown failed", { error: errorText(error) });
    });
    await lifecycleTail;
    ctx = undefined;
  });
}
