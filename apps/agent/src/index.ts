/** Entry point. Connects Postgres and Baileys, resets any command left
 * CLAIMED by this same process's previous crashed run (exactly one agent
 * is ever supposed to run -- see docs/whatsapp-agent-setup.md on
 * -MultipleInstances IgnoreNew), then loops: claim the oldest eligible
 * PENDING command and process it, woken either by a Postgres NOTIFY or a
 * fixed fallback poll, whichever comes first.
 */
import "dotenv/config";
import { AgentDb } from "./db";
import { BaileysAgent, AgentConnectionState } from "./baileys";
import { CircuitBreaker } from "./circuitBreaker";
import { RateLimiter } from "./rateLimiter";
import { processCommand } from "./commands";
import { scheduleCanary } from "./canary";
import { logger } from "./logger";

const AGENT_VERSION = "0.1.0";
const HEARTBEAT_INTERVAL_MS = 30_000;
const POLL_INTERVAL_MS = 500;
const CLAIM_MARGIN_SECONDS = 2;
const WORKER_ID = `local-agent-${process.pid}`;

async function main(): Promise<void> {
  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) {
    throw new Error(
      "DATABASE_URL is not set -- run scripts/setup-agent-role.ps1 first " +
        "(see docs/whatsapp-agent-setup.md)."
    );
  }

  const db = new AgentDb(databaseUrl);
  await db.resetStaleClaims();

  const rateLimiter = new RateLimiter();
  const breaker = new CircuitBreaker();

  const heartbeat = (state: AgentConnectionState, detail: string): void => {
    db.upsertStatus({
      state,
      detail,
      agentVersion: AGENT_VERSION,
      lastSuccessfulSendAt: null,
      lastCanaryAt: null,
      lastCanaryOk: null,
    }).catch((err: unknown) => logger.error({ err }, "status.upsert_failed"));
  };

  const baileys = await BaileysAgent.connect({ db, onStateChange: heartbeat });

  const heartbeatTimer = setInterval(
    () => heartbeat(baileys.currentState(), "heartbeat"),
    HEARTBEAT_INTERVAL_MS
  );
  const stopCanary = scheduleCanary({ db, baileys });

  let wake: (() => void) | null = null;
  const waitForWake = (): Promise<void> =>
    new Promise((resolve) => {
      wake = resolve;
    });

  db.listenForCommands(() => wake?.()).catch((err: unknown) => {
    logger.warn({ err }, "listen.failed -- relying on the fallback poll only");
  });

  logger.info({ version: AGENT_VERSION, workerId: WORKER_ID }, "agent.started");

  process.on("SIGINT", () => shutdown(0));
  process.on("SIGTERM", () => shutdown(0));

  function shutdown(code: number): void {
    logger.info("agent.stopping");
    clearInterval(heartbeatTimer);
    stopCanary();
    db.close()
      .catch((err: unknown) => logger.error({ err }, "db.close_failed"))
      .finally(() => process.exit(code));
  }

  for (;;) {
    const claimed = await db.claimNext(WORKER_ID, CLAIM_MARGIN_SECONDS).catch((err: unknown) => {
      logger.error({ err }, "claim_next.failed");
      return null;
    });

    if (claimed) {
      await processCommand(claimed, { db, baileys, rateLimiter, breaker }).catch(
        (err: unknown) => logger.error({ err, commandId: claimed.id }, "command.crashed")
      );
      continue;
    }

    await Promise.race([
      waitForWake(),
      new Promise<void>((resolve) => setTimeout(resolve, POLL_INTERVAL_MS)),
    ]);
  }
}

main().catch((err: unknown) => {
  logger.fatal({ err }, "agent.fatal");
  process.exit(1);
});
