/** Trips after consecutive send failures that look like automation
 * detection, so the agent stops hammering WhatsApp into a ban instead of
 * retrying blindly (Phase 0 risk R4). No persisted "tripped" state -- the
 * counter is in-memory and resets on a fresh process; recovery is an
 * operator restart after investigating (see docs/whatsapp-agent-setup.md).
 */
export class CircuitBreaker {
  private consecutiveFailures = 0;

  constructor(private readonly threshold: number = 5) {}

  isOpen(): boolean {
    return this.consecutiveFailures >= this.threshold;
  }

  recordSuccess(): void {
    this.consecutiveFailures = 0;
  }

  recordFailure(): void {
    this.consecutiveFailures += 1;
  }

  failureCount(): number {
    return this.consecutiveFailures;
  }
}
