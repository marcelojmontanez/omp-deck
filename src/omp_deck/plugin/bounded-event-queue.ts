export class BoundedEventQueue {
  private tail = Promise.resolve();
  private count = 0;
  private bytes = 0;
  private readonly maxCount: number;
  private readonly maxBytes: number;
  private readonly disconnected: () => boolean;
  private readonly reject: () => void;

  constructor(maxCount: number, maxBytes: number, disconnected: () => boolean, reject: () => void) {
    this.maxCount = maxCount;
    this.maxBytes = maxBytes;
    this.disconnected = disconnected;
    this.reject = reject;
  }

  enqueue(weight: number, operation: () => Promise<boolean>): Promise<boolean> {
    if (this.disconnected() || this.count >= this.maxCount || this.bytes + weight > this.maxBytes) {
      if (!this.disconnected()) this.reject();
      return Promise.resolve(false);
    }
    this.count++;
    this.bytes += weight;
    const result = this.tail.then(operation, () => false);
    this.tail = result.then(
      () => { this.count--; this.bytes -= weight; },
      () => { this.count--; this.bytes -= weight; },
    );
    return result;
  }
}
