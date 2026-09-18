export type LineReadResult = "ok" | "overflow" | "stopped";

export class BoundedLineReader {
  private chunks: Buffer[] = [];
  private bytes = 0;
  private readonly maxBytes: number;
  private readonly maxChunks: number;

  constructor(maxBytes: number, maxChunks = 1024) {
    if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) throw new Error("maxBytes must be positive");
    if (!Number.isSafeInteger(maxChunks) || maxChunks <= 1) throw new Error("maxChunks must exceed one");
    this.maxBytes = maxBytes;
    this.maxChunks = maxChunks;
  }

  push(chunk: Buffer, onLine: (line: Buffer) => boolean): LineReadResult {
    let offset = 0;
    while (offset < chunk.length) {
      const newline = chunk.indexOf(10, offset);
      const end = newline < 0 ? chunk.length : newline;
      const segment = chunk.subarray(offset, end);
      if (this.bytes + segment.length > this.maxBytes) return "overflow";
      if (segment.length) {
        this.chunks.push(segment);
        this.bytes += segment.length;
        if (this.chunks.length >= this.maxChunks) {
          this.chunks = [Buffer.concat(this.chunks, this.bytes)];
        }
      }
      if (newline < 0) return "ok";
      const line = this.takeLine();
      if (!onLine(line)) return "stopped";
      offset = newline + 1;
    }
    return "ok";
  }

  private takeLine(): Buffer {
    const line = this.chunks.length === 0
      ? Buffer.alloc(0)
      : this.chunks.length === 1
        ? this.chunks[0]
        : Buffer.concat(this.chunks, this.bytes);
    this.chunks = [];
    this.bytes = 0;
    return line;
  }
}
