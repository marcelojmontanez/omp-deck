import { createHash, randomBytes, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { authenticateNamedPipe } from "./named-pipe-auth.ts";
import { BoundedSocketWriter } from "./bounded-writer.ts";
import { BoundedEventQueue } from "./bounded-event-queue.ts";
import { BoundedLineReader } from "./bounded-line-reader.ts";
import { BoundedRequestDispatcher } from "./bounded-request-dispatcher.ts";
import { captureHistory, projectMessage, projectTodoResult, streamHistory } from "./history-sync.ts";
import { deliverUserMessage } from "./image-content.ts";
import { REGISTRATION_ATTACHMENT_LIMITS, REGISTRATION_CAPABILITIES } from "./registration.ts";
import { degradedSnapshot, readThinkingState, setThinking } from "./thinking-state.ts";
const PROTOCOL = "omp-deck-fallback-v1";
const MAX_LINE_BYTES = 1024 * 1024;
const MAX_REQUEST_LINE_BYTES = 32 * 1024 * 1024;
const HEARTBEAT_MS = 15_000;
const MAX_MISSED_HEARTBEATS = 3;
const MAX_QUEUED_REQUESTS = 64;
const MAX_QUEUED_REQUEST_BYTES = 2 * MAX_REQUEST_LINE_BYTES;
const MAX_QUEUED_EVENTS = 256;
const MAX_PENDING_WRITE_BYTES = 4 * MAX_LINE_BYTES;
const WINDOWS = process.platform === "win32";
const UNIX_SOCKET_PATH_BYTES = process.platform === "linux" ? 108 : 104;
const SOCKET_DIR = WINDOWS
  ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "OMP Deck", "state", "sockets")
  : path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"), "omp-deck", "sockets");
type JsonObject = Record<string, unknown>;
type ManagedTimer = Parameters<ExtensionContext["clearTimer"]>[0];
interface Peer {
  socket: net.Socket;
  input: BoundedLineReader;
  lastPong: number;
  requests: BoundedRequestDispatcher<JsonObject>;
  sequence: number;
  writer: BoundedSocketWriter;
  events: BoundedEventQueue;
}
function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
export default function ompDeck(pi: ExtensionAPI) {
  const connectionId = randomUUID();
  const peers = new Set<Peer>();
  const pendingAuthentication = new Set<net.Socket>();
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
      thinkingLevels: readThinkingState(pi, context).thinkingLevels,
      attachmentLimits: REGISTRATION_ATTACHMENT_LIMITS,
      capabilities: REGISTRATION_CAPABILITIES,
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
  function send(peer: Peer, payload: JsonObject): Promise<boolean> {
    const line = encoded(payload);
    return line === undefined ? Promise.resolve(false) : peer.writer.write(line);
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
  async function sendEventNow(peer: Peer, kind: string, data: unknown = {}): Promise<boolean> {
    const sequence = peer.sequence + 1;
    let wireKind = kind;
    let wireData = data;
    let line = eventLine(peer, wireKind, wireData);
    let degraded = false;
    if (!line) {
      degraded = true;
      if (kind === "snapshot") {
        wireData = degradedSnapshot(data);
      } else {
        wireKind = "degraded";
        wireData = { droppedKind: kind, reason: "event exceeds wire limit" };
      }
      line = eventLine(peer, wireKind, wireData);
    }
    if (!line) return false;
    peer.sequence = sequence;
    if (!await peer.writer.write(line)) return false;
    if (degraded && kind === "snapshot") {
      await sendEventNow(peer, "degraded", {
        droppedKind: kind,
        reason: "snapshot collections exceed wire limit",
      });
    }
    return true;
  }
  function sendEvent(peer: Peer, kind: string, data: unknown = {}): Promise<boolean> {
    let queuedKind = kind;
    let queuedData = data;
    let line = eventLine(peer, queuedKind, queuedData);
    let snapshotDegraded = false;
    if (!line) {
      if (kind === "snapshot") {
        queuedData = degradedSnapshot(data);
        snapshotDegraded = true;
      } else {
        queuedKind = "degraded";
        queuedData = { droppedKind: kind, reason: "event exceeds wire limit" };
      }
      line = eventLine(peer, queuedKind, queuedData);
    }
    if (!line) return Promise.resolve(false);
    const bytes = Buffer.byteLength(line);
    return peer.events.enqueue(bytes, async () => {
      const sent = await sendEventNow(peer, queuedKind, queuedData);
      if (sent && snapshotDegraded) {
        await sendEventNow(peer, "degraded", {
          droppedKind: kind,
          reason: "snapshot collections exceed wire limit",
        });
      }
      return sent;
    });
  }
  function emit(kind: string, data: unknown = {}): void {
    for (const peer of peers) void sendEvent(peer, kind, data);
  }
  function sendHistory(peer: Peer, messages: readonly unknown[]): Promise<boolean> {
    return peer.events.enqueue(0, async () => {
      const syncId = `${connectionId}:${++historyGeneration}`;
      const completed = await streamHistory(
        messages,
        syncId,
        () => peer.socket.destroyed,
        (data) => eventLine(peer, "history", data) !== undefined,
        (data) => sendEventNow(peer, "history", data),
      );
      if (!completed && !peer.socket.destroyed) {
        await sendEventNow(peer, "degraded", {
          droppedKind: "history",
          reason: "history message exceeds wire limit",
        });
      }
      return completed;
    });
  }
  function emitHistory(context: ExtensionContext): void {
    const messages = captureHistory(context);
    for (const peer of peers) void sendHistory(peer, messages);
  }
  function snapshots(context: ExtensionContext): JsonObject {
    return {
      idle: context.isIdle(),
      pendingMessages: context.hasPendingMessages(),
      model: context.model ? { provider: context.model.provider, id: context.model.id } : null,
      async: context.getAsyncJobSnapshot(),
      ...readThinkingState(pi, context),
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
          const result = deliverUserMessage(pi, active, request);
          await send(peer, { protocol: PROTOCOL, type: "response", id, ok: true, result });
          return;
        }
        case "interrupt":
          active.abort();
          break;
        case "shutdown":
          if (await send(peer, { protocol: PROTOCOL, type: "response", id, ok: true })) active.shutdown();
          return;
        case "setThinking": {
          const thinking = setThinking(pi, active, request.level);
          emit("thinking", thinking);
          const result = { ...thinking, eventSequence: peer.sequence };
          await send(peer, { protocol: PROTOCOL, type: "response", id, ok: true, result });
          return;
        }
        case "snapshot": {
          const response = {
            protocol: PROTOCOL,
            type: "response",
            id,
            ok: true,
            result: { ...snapshots(active), eventSequence: peer.sequence },
          };
          if (!await send(peer, response)) {
            await send(peer, {
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
          await sendHistory(peer, captureHistory(active));
          break;
        case "pong":
          peer.lastPong = Date.now();
          break;
        default:
          throw new Error("unsupported action");
      }
      await send(peer, { protocol: PROTOCOL, type: "response", id, ok: true });
    } catch (error) {
      await send(peer, { protocol: PROTOCOL, type: "response", id, ok: false, error: errorText(error) });
    }
  }
  function attach(socket: net.Socket, context: ExtensionContext): void {
    const peer: Peer = {
      socket,
      input: new BoundedLineReader(MAX_REQUEST_LINE_BYTES),
      lastPong: Date.now(),
      requests: new BoundedRequestDispatcher(
        MAX_QUEUED_REQUESTS,
        MAX_QUEUED_REQUEST_BYTES,
        (request) => dispatch(peer, request),
        (error, priority) => pi.logger.warn(
          priority ? "OMP Deck fallback priority request failed" : "OMP Deck fallback request failed",
          { error: errorText(error) },
        ),
      ),
      sequence: 0,
      writer: new BoundedSocketWriter(socket, MAX_PENDING_WRITE_BYTES, (error) => {
        pi.logger.warn("OMP Deck fallback write failed", { error: errorText(error) });
      }),
      events: new BoundedEventQueue(
        MAX_QUEUED_EVENTS,
        MAX_PENDING_WRITE_BYTES,
        () => socket.destroyed,
        () => socket.destroy(new Error("event queue limit exceeded")),
      ),
    };
    peers.add(peer);
    const registrationLine = encoded(registration(context));
    if (!registrationLine) {
      peers.delete(peer);
      socket.destroy(new Error("registration exceeds wire limit"));
      return;
    }
    void peer.writer.write(registrationLine);
    sendEvent(peer, "snapshot", snapshots(context));
    const handleLine = (line: Buffer): boolean => {
      if (!line.length) return true;
      try {
        const parsed: unknown = JSON.parse(line.toString("utf8"));
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("request must be an object");
        const request = parsed as JsonObject;
        const priority =
          request.action === "pong" || request.action === "interrupt" || request.action === "shutdown";
        if (!peer.requests.submit(request, line.length, priority)) {
          send(peer, { protocol: PROTOCOL, type: "response", id: null, ok: false, error: "request queue limit exceeded" });
          socket.destroy();
          return false;
        }
      } catch (error) {
        send(peer, { protocol: PROTOCOL, type: "response", id: null, ok: false, error: `invalid JSON: ${errorText(error)}` });
      }
      return true;
    };
    socket.on("data", (chunk: Buffer) => {
      const result = peer.input.push(chunk, handleLine);
      if (result === "overflow") {
        send(peer, { protocol: PROTOCOL, type: "response", id: null, ok: false, error: "line exceeds 32 MiB" });
        socket.destroy();
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
    pendingAuthentication.add(socket);
    const authenticationEnded = (): void => { pendingAuthentication.delete(socket); };
    socket.once("close", authenticationEnded);
    authenticateNamedPipe(socket, context, windowsToken, (remainder) => {
      pendingAuthentication.delete(socket);
      socket.off("close", authenticationEnded);
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
    for (const socket of pendingAuthentication) socket.destroy();
    pendingAuthentication.clear();
    if (active) await new Promise<void>((resolve) => active.close(() => resolve()));
    await removeFiles();
  }
  function scheduleReconnect(context: ExtensionContext, generation: number): void {
    if (stopping || retryTimer || generation !== lifecycleGeneration) return;
    const delay = Math.min(30_000, 250 * (2 ** Math.min(reconnectAttempt++, 7)));
    retryTimer = context.setTimeout(() => {
      if (stopping || generation !== lifecycleGeneration) return;
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
          .then(() => scheduleReconnect(context, generation))
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
      scheduleReconnect(context, generation);
    }
  }
  function queueRestart(context: ExtensionContext): Promise<void> {
    // RPC owns its structured transport; only terminal hosts need this fallback.
    if (context.mode !== "tui") return lifecycleTail;
    if (retryTimer) context.clearTimer(retryTimer);
    retryTimer = undefined;
    const generation = ++lifecycleGeneration;
    lifecycleTail = lifecycleTail.then(() => start(context, generation)).catch((error) => {
      pi.logger.warn("OMP Deck fallback lifecycle failed", { error: errorText(error) });
    });
    return lifecycleTail;
  }
  function startHeartbeat(context: ExtensionContext): void {
    if (context.mode !== "tui" || heartbeatTimer) return;
    heartbeatTimer = context.setInterval(() => {
      const now = Date.now();
      for (const peer of peers) {
        if (now - peer.lastPong > HEARTBEAT_MS * MAX_MISSED_HEARTBEATS) peer.socket.destroy();
        else sendEvent(peer, "heartbeat", { time: now, ...readThinkingState(pi, context) });
      }
    }, HEARTBEAT_MS);
  }
  function toolEvent(phase: string, event: { toolCallId: string; toolName: string; intent?: string; result?: unknown }): void {
    const terminalOnly = event.toolName.toLowerCase() === "ask";
    emit("tool", { phase, toolCallId: event.toolCallId, toolName: event.toolName, intent: event.intent,
      ...(terminalOnly ? { terminalOnly: true, instruction: "Answer this request in the terminal" } : {}) });
    const todos = phase === "end" ? projectTodoResult(event.toolName, event.result) : undefined;
    if (todos) emit("todo", { todos });
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
  pi.on("agent_start", () => emit("lifecycle", { state: "agent_start" }));
  pi.on("turn_start", (_event, context) => emit("thinking", readThinkingState(pi, context)));
  pi.on("agent_end", (_event, context) => emit("lifecycle", { state: "agent_end", snapshot: snapshots(context) }));
  pi.on("message_start", () => emit("message", { phase: "start" }));
  pi.on("message_update", () => emit("message", { phase: "update" }));
  pi.on("message_end", (event) => emit("message", { phase: "end", message: projectMessage(event.message) }));
  pi.on("tool_execution_start", (event) => toolEvent("start", event));
  pi.on("tool_execution_update", (event) => toolEvent("update", event));
  pi.on("tool_execution_end", (event) => toolEvent("end", event));
  pi.on("todo_reminder", (event) => emit("todo", { todos: event.todos, attempt: event.attempt }));
  pi.on("turn_end", (_event, context) => emit("async", { snapshot: context.getAsyncJobSnapshot() }));
  pi.on("session_shutdown", async () => {
    stopping = true;
    lifecycleGeneration++;
    emit("lifecycle", { state: "session_shutdown" });
    lifecycleTail = lifecycleTail.then(() => closeServer()).catch((error) => {
      pi.logger.warn("OMP Deck fallback shutdown failed", { error: errorText(error) });
    });
    await lifecycleTail;
    if (ctx && retryTimer) ctx.clearTimer(retryTimer);
    if (ctx && heartbeatTimer) ctx.clearTimer(heartbeatTimer);
    retryTimer = undefined;
    heartbeatTimer = undefined;
    ctx = undefined;
  });
}
