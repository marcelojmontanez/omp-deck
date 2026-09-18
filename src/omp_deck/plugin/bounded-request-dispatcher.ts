export class BoundedRequestDispatcher<T> {
  private count = 0;
  private bytes = 0;
  private tail = Promise.resolve();
  private readonly maxCount: number;
  private readonly maxBytes: number;
  private readonly dispatch: (value: T) => Promise<void>;
  private readonly onError: (error: unknown, priority: boolean) => void;

  constructor(
    maxCount: number,
    maxBytes: number,
    dispatch: (value: T) => Promise<void>,
    onError: (error: unknown, priority: boolean) => void,
  ) {
    this.maxCount = maxCount;
    this.maxBytes = maxBytes;
    this.dispatch = dispatch;
    this.onError = onError;
  }

  submit(value: T, bytes: number, priority: boolean): boolean {
    if (this.count >= this.maxCount || this.bytes + bytes > this.maxBytes) return false;
    this.count++;
    this.bytes += bytes;
    const run = priority ? this.dispatch(value) : this.tail.then(() => this.dispatch(value));
    const settled = run
      .catch((error) => this.onError(error, priority))
      .finally(() => {
        this.count--;
        this.bytes -= bytes;
      });
    if (priority) void settled;
    else this.tail = settled;
    return true;
  }
}
