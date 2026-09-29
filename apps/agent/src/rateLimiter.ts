/** Enforces a minimum gap between sends and a rolling hourly cap. Claiming
 * one send_text row at a time already serializes sends (see db.ts); this
 * adds an explicit floor so a queue that backs up does not leave WhatsApp
 * the moment it drains (Phase 0 risk R4's rate-limiting mitigation).
 *
 * `now` is injected so this is testable without real sleeps -- see
 * test/rateLimiter.test.ts.
 */
export class RateLimiter {
  private lastSendAt = -Infinity;
  private sendTimestamps: number[] = [];

  constructor(
    private readonly minIntervalMs: number = 3000,
    private readonly maxPerHour: number = 200,
    private readonly now: () => number = Date.now,
    private readonly sleep: (ms: number) => Promise<void> = (ms) =>
      new Promise((resolve) => setTimeout(resolve, ms))
  ) {}

  /** How long a call right now would have to wait before it may send.
   * Exposed separately from waitForTurn so a test can assert the delay
   * without actually waiting it out. */
  msUntilNextTurn(): number {
    return Math.max(0, this.minIntervalMs - (this.now() - this.lastSendAt));
  }

  /** Resolves once it is this caller's turn, then reserves that turn --
   * two overlapping calls each wait for their own, non-overlapping slot. */
  async waitForTurn(): Promise<void> {
    const wait = this.msUntilNextTurn();
    if (wait > 0) {
      await this.sleep(wait);
    }
    this.lastSendAt = this.now();
  }

  /** Whether a send may start without exceeding the rolling hourly cap.
   * Does not itself wait -- a caller over the cap should leave the row for
   * a later tick rather than block here indefinitely. */
  canSendNow(): boolean {
    const cutoff = this.now() - 3_600_000;
    this.sendTimestamps = this.sendTimestamps.filter((t) => t > cutoff);
    return this.sendTimestamps.length < this.maxPerHour;
  }

  recordSend(): void {
    this.sendTimestamps.push(this.now());
  }
}
