import type * as net from "node:net";
const MAX_PENDING_FRAMES = 256;

export class BoundedSocketWriter {
  private tail = Promise.resolve();
  private failed = false;
  private pendingBytes = 0;
  private pendingFrames = 0;
  private readonly socket: net.Socket;
  private readonly maxPendingBytes: number;
  private readonly onError: (error: unknown) => void;

  constructor(socket: net.Socket, maxPendingBytes: number, onError: (error: unknown) => void) {
    this.socket = socket;
    this.maxPendingBytes = maxPendingBytes;
    this.onError = onError;
  }

  async write(line: string): Promise<boolean> {
    const bytes = Buffer.byteLength(line);
    if (this.socket.destroyed || this.failed) return false;
    if (
      bytes > this.maxPendingBytes
      || this.pendingBytes + bytes > this.maxPendingBytes
      || this.pendingFrames >= MAX_PENDING_FRAMES
    ) {
      this.failed = true;
      this.onError(new Error("outbound queue limit exceeded"));
      this.socket.destroy();
      return false;
    }
    this.pendingBytes += bytes;
    this.pendingFrames++;
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise<void>((resolve) => { release = resolve; });
    await previous;
    try {
      if (this.socket.destroyed || this.failed) return false;
      if (!this.socket.write(line)) await this.waitForDrain();
      return !this.socket.destroyed;
    } catch (error) {
      this.failed = true;
      this.onError(error);
      this.socket.destroy();
      return false;
    } finally {
      this.pendingBytes -= bytes;
      this.pendingFrames--;
      release();
    }
  }

  private waitForDrain(): Promise<void> {
    return new Promise<void>((resolve) => {
      const finish = (): void => {
        this.socket.off("drain", finish);
        this.socket.off("close", finish);
        this.socket.off("error", finish);
        resolve();
      };
      this.socket.once("drain", finish);
      this.socket.once("close", finish);
      this.socket.once("error", finish);
    });
  }
}
