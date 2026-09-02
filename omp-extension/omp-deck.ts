import * as fs from "node:fs";
import { randomUUID } from "node:crypto";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const SOCKET_DIR = path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"), "omp-deck", "sockets");
const MAX_REQUEST_BYTES = 64 * 1024;
const IMAGE_TYPES: Record<string, string> = {
  ".gif": "image/gif",
  ".jpeg": "image/jpeg",
  ".jpg": "image/jpeg",
  ".png": "image/png",
  ".webp": "image/webp",
};

interface BridgeRequest {
  message?: unknown;
  attachments?: unknown;
  thinking?: unknown;
}

export default function ompDeck(pi: ExtensionAPI) {
  let server: net.Server | undefined;
  let socketPath: string | undefined;
  let metadataPath: string | undefined;
  let registeredSession = "";
  let syncing = false;
  let syncTimer: NodeJS.Timeout | undefined;
  const instanceId = randomUUID().slice(0, 8);

  async function stopServer() {
    const active = server;
    server = undefined;
    if (active) await new Promise<void>((resolve) => active.close(() => resolve()));
    if (metadataPath) {
      await fs.promises.unlink(metadataPath).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== "ENOENT") throw error;
      });
      metadataPath = undefined;
    }
    if (socketPath) {
      await fs.promises.unlink(socketPath).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== "ENOENT") throw error;
      });
      socketPath = undefined;
    }
    registeredSession = "";
  }

  async function startServer(ctx: ExtensionContext) {
    await stopServer();
    await fs.promises.mkdir(SOCKET_DIR, { recursive: true, mode: 0o700 });
    await fs.promises.chmod(SOCKET_DIR, 0o700);
    socketPath = path.join(SOCKET_DIR, `${ctx.sessionManager.getSessionId()}.${process.pid}.${instanceId}.sock`);

    metadataPath = `${socketPath}.json`;
    server = net.createServer((socket) => {
      let input = Buffer.alloc(0);
      let handled = false;

      const reply = (payload: object) => {
        if (!socket.destroyed) socket.end(`${JSON.stringify(payload)}\n`);
      };

      socket.on("data", (chunk) => {
        if (handled) return;
        input = Buffer.concat([input, chunk]);
        if (input.length > MAX_REQUEST_BYTES) {
          handled = true;
          reply({ ok: false, error: "request too large" });
          return;
        }
        const newline = input.indexOf(10);
        if (newline === -1) return;
        handled = true;
        void (async () => {
          try {
            const request = JSON.parse(input.subarray(0, newline).toString("utf8")) as BridgeRequest;
            const message = typeof request.message === "string" ? request.message.trim() : "";
            const attachments = Array.isArray(request.attachments)
              ? request.attachments.filter((value): value is string => typeof value === "string")
              : [];
            if (!message || message.length > 4000) throw new Error("invalid message");
            if (attachments.length > 4) throw new Error("too many attachments");
            if (request.thinking != null) {
              const thinking = String(request.thinking);
              if (!["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"].includes(thinking)) {
                throw new Error("invalid thinking level");
              }
              pi.setThinkingLevel(thinking as Parameters<typeof pi.setThinkingLevel>[0]);
            }
            const imageFiles = attachments.filter((file) => IMAGE_TYPES[path.extname(file).toLowerCase()]);
            const otherFiles = attachments.filter((file) => !IMAGE_TYPES[path.extname(file).toLowerCase()]);
            const text = `${otherFiles.map((file) => `@${file}`).join(" ")}${otherFiles.length ? "\n" : ""}${message}`;
            const images = await Promise.all(imageFiles.map(async (file) => ({
              type: "image" as const,
              data: (await fs.promises.readFile(file)).toString("base64"),
              mimeType: IMAGE_TYPES[path.extname(file).toLowerCase()],
            })));
            const content = images.length ? [...images, { type: "text" as const, text }] : text;
            if (ctx.isIdle()) await pi.sendUserMessage(content);
            else await pi.sendUserMessage(content, { deliverAs: "followUp" });
            reply({ ok: true });
          } catch (error) {
            reply({ ok: false, error: error instanceof Error ? error.message : String(error) });
          }
        })();
      });
      socket.on("error", (error) => pi.logger.warn("OMP Deck socket error", { error: error.message }));
    });
    server.on("error", (error) => pi.logger.error("OMP Deck listener failed", { error: error.message }));
    await new Promise<void>((resolve, reject) => {
      server!.once("error", reject);
      server!.listen(socketPath, () => {
        server!.off("error", reject);
        resolve();
      });
    });
    await fs.promises.chmod(socketPath, 0o600);
    const model = ctx.model;
    await fs.promises.writeFile(metadataPath, JSON.stringify({
      sessionId: ctx.sessionManager.getSessionId(),
      sessionFile: ctx.sessionManager.getSessionFile() || null,
      cwd: ctx.cwd,
      pid: process.pid,
      title: pi.getSessionName() || "",
      model: model ? `${model.provider}/${model.id}` : null,
      thinking: pi.getThinkingLevel(),
      registeredAt: new Date().toISOString(),
    }), { mode: 0o600 });
    registeredSession = `${ctx.sessionManager.getSessionId()}\0${ctx.sessionManager.getSessionFile() || ""}`;
  }

  async function ensureServer(ctx: ExtensionContext) {
    const session = `${ctx.sessionManager.getSessionId()}\0${ctx.sessionManager.getSessionFile() || ""}`;
    const registrationExists = socketPath && metadataPath
      && fs.existsSync(socketPath) && fs.existsSync(metadataPath);
    if (server?.listening && registrationExists && session === registeredSession) return;
    await startServer(ctx);
  }

  function watchSession(ctx: ExtensionContext) {
    if (syncTimer) return;
    syncTimer = setInterval(() => {
      if (syncing) return;
      syncing = true;
      void ensureServer(ctx)
        .catch((error) => pi.logger.warn("OMP Deck session sync failed", {
          error: error instanceof Error ? error.message : String(error),
        }))
        .finally(() => { syncing = false; });
    }, 1000);
    syncTimer.unref();
  }

  pi.registerCommand("omp-deck", {
    description: "Reconnect this OMP to OMP Deck",
    handler: async (_args, ctx) => {
      await startServer(ctx);
      ctx.ui.notify("OMP Deck connected", "info");
    },
  });
  pi.on("session_start", async (_event, ctx) => {
    await ensureServer(ctx);
    watchSession(ctx);
  });
  pi.on("session_switch", async (_event, ctx) => startServer(ctx));
  pi.on("session_branch", async (_event, ctx) => startServer(ctx));
  pi.on("input", async (_event, ctx) => ensureServer(ctx));
  pi.on("agent_start", async (_event, ctx) => ensureServer(ctx));
  pi.on("session_shutdown", async () => {
    if (syncTimer) clearInterval(syncTimer);
    syncTimer = undefined;
    await stopServer();
  });
}
