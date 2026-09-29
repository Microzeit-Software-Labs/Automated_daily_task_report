/** Weekly self-test: enqueues a test_send to the linked account's own
 * number ("Message Yourself"), through the same outbox every other command
 * uses, so it gets a uniform, queryable history rather than a side
 * channel -- the normal claim loop (index.ts) picks it up like any other
 * row. Exists to catch "looks connected but sends are silently failing"
 * (Phase 0 risk R4) before a human notices reports have stopped arriving.
 */
import { AgentDb } from "./db";
import { BaileysAgent } from "./baileys";
import { logger } from "./logger";

const CANARY_INTERVAL_MS = 7 * 24 * 60 * 60 * 1000;
const CANARY_COMMAND_TIMEOUT_SECONDS = 30;

export function scheduleCanary(opts: { db: AgentDb; baileys: BaileysAgent }): () => void {
  const timer = setInterval(() => {
    void enqueueCanary(opts);
  }, CANARY_INTERVAL_MS);
  return () => clearInterval(timer);
}

async function enqueueCanary(opts: { db: AgentDb; baileys: BaileysAgent }): Promise<void> {
  if (opts.baileys.currentState() !== "CONNECTED" || !opts.baileys.ownJid()) {
    logger.warn("canary.skipped_not_connected");
    return;
  }
  try {
    const commandId = await opts.db.enqueueTestSend(
      "weekly_canary",
      CANARY_COMMAND_TIMEOUT_SECONDS
    );
    logger.info({ commandId }, "canary.enqueued");
  } catch (err) {
    logger.error({ err }, "canary.enqueue_failed");
  }
}
