/** Entry point. Takes the one-agent-per-database lock, resets any command left
 * CLAIMED by this same process's previous crashed run, connects WhatsApp (if a
 * phone is linked), then loops: claim the oldest eligible PENDING command and
 * process it, woken either by a Postgres NOTIFY or a fixed fallback poll,
 * whichever comes first.
 *
 * While WhatsApp is not connected the loop claims only link-management
 * commands (link a phone, reconnect): it could not send a message or look up a
 * group anyway, and claiming them would leave them stuck.
 */
import "dotenv/config";
import { join } from "node:path";
import qrcodeTerminal from "qrcode-terminal";
import { AgentDb } from "./db";
import { BaileysAgent, openPairingSocket } from "./baileys";
import { CircuitBreaker } from "./circuitBreaker";
import { RateLimiter } from "./rateLimiter";
import { processCommand } from "./commands";
import { scheduleCanary } from "./canary";
import { LinkController } from "./linkController";
import { removeDirQuiet, SessionStore } from "./sessionFiles";
import { logger } from "./logger";

const AGENT_VERSION = "0.2.0";
const HEARTBEAT_INTERVAL_MS = 30_000;
const POLL_INTERVAL_MS = 500;
const CLAIM_MARGIN_SECONDS = 2;
const WORKER_ID = `local-agent-${process.pid}`;
const SHUTDOWN_GRACE_MS = 3_000;
/** Distinct from a crash (1) so a supervisor can tell "already running" from "broke". */
const ALREADY_RUNNING_EXIT_CODE = 3;

async function main(): Promise<void> {
  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) {
    throw new Error(
      "DATABASE_URL is not set -- run scripts/setup-agent-role.ps1 first " +
        "(see docs/whatsapp-agent-setup.md)."
    );
  }

  let stopping = false;

  const db = new AgentDb(databaseUrl, {
    onLockLost: (err) => {
      if (stopping) {
        return;
      }
      // The one-agent lock lived on that connection and is now released, so
      // carrying on could let a second agent start beside this one. Restarting is
      // the safe answer (the supervisor does it within seconds): do it on purpose,
      // and say why, instead of dying on an uncaught exception.
      logger.fatal({ err }, "db.lock_connection_lost -- exiting so the supervisor restarts the agent");
      process.exit(1);
    },
    onNonFatalError: (err, what) =>
      logger.warn(
        { err },
        what === "listen"
          ? "listen.connection_lost -- relying on the fallback poll only"
          : "db.idle_connection_error -- the pool will replace it"
      ),
  });

  // Before anything that assumes it is the only agent. reset_stale_claims
  // below would otherwise steal a live peer's in-flight work.
  if (!(await db.acquireInstanceLock())) {
    logger.error(
      "agent.already_running -- another Interlock WhatsApp agent holds the database lock; exiting"
    );
    await db.close();
    process.exit(ALREADY_RUNNING_EXIT_CODE);
  }
  await db.resetStaleClaims();

  // dist/src/ -> apps/agent/. Sessions must not live under dist/: a clean
  // rebuild would silently wipe the pairing and force a new QR scan.
  const agentDir = join(__dirname, "..", "..");
  const sessions = new SessionStore(agentDir);
  // Tidy up, best-effort (a folder Windows still has open is left for next time):
  // the scratch folder the first version of linking used, and old session folders.
  removeDirQuiet(join(agentDir, ".wa-session.pairing"));
  const tidied = sessions.prune();
  if (tidied.length > 0) {
    logger.info({ tidied }, "agent.old_sessions_removed");
  }
  await db.updatePairing({ state: "IDLE", pairingId: null, qr: null, qrAt: null, detail: "" });

  const rateLimiter = new RateLimiter();
  const breaker = new CircuitBreaker();

  const baileys = await BaileysAgent.connect({
    db,
    store: sessions,
    onStateChange: (state, detail, reason, account) => {
      logger.info({ state, reason, detail }, "agent.state");
      db.upsertStatus({
        state,
        detail,
        reason,
        accountJid: account?.jid ?? null,
        accountName: account?.name ?? null,
        agentVersion: AGENT_VERSION,
        lastSuccessfulSendAt: null,
        lastCanaryAt: null,
        lastCanaryOk: null,
      }).catch((err: unknown) => logger.error({ err }, "status.upsert_failed"));
    },
  });

  const link = new LinkController({
    store: db,
    adopter: baileys,
    openSocket: openPairingSocket,
    newSessionDir: () => sessions.newGenerationDir(),
    onQr: (qr) => {
      if (process.stdout.isTTY) {
        qrcodeTerminal.generate(qr, { small: true });
      }
    },
    onError: (err, what) => logger.error({ err }, `link.${what}_failed`),
  });

  const heartbeatTimer = setInterval(() => {
    const account = baileys.account();
    db.upsertStatus({
      state: baileys.currentState(),
      detail: "heartbeat",
      reason: baileys.currentReason(),
      accountJid: account?.jid ?? null,
      accountName: account?.name ?? null,
      agentVersion: AGENT_VERSION,
      lastSuccessfulSendAt: null,
      lastCanaryAt: null,
      lastCanaryOk: null,
    }).catch((err: unknown) => logger.error({ err }, "status.upsert_failed"));
  }, HEARTBEAT_INTERVAL_MS);
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
    if (stopping) {
      return; // a second Ctrl+C while already stopping
    }
    stopping = true;
    logger.info("agent.stopping");
    clearInterval(heartbeatTimer);
    stopCanary();
    wake?.();
    // Backstop: whatever hangs, the process still exits.
    setTimeout(() => process.exit(code), SHUTDOWN_GRACE_MS).unref();
    try {
      link.shutdown();
      baileys.close();
    } catch (err: unknown) {
      logger.error({ err }, "baileys.close_failed");
    }
    db.close()
      .catch((err: unknown) => logger.error({ err }, "db.close_failed"))
      .finally(() => process.exit(code));
  }

  while (!stopping) {
    const connected = baileys.currentState() === "CONNECTED";
    const claim = connected ? db.claimNext.bind(db) : db.claimNextControl.bind(db);
    const claimed = await claim(WORKER_ID, CLAIM_MARGIN_SECONDS).catch((err: unknown) => {
      logger.error({ err }, "claim_next.failed");
      return null;
    });

    if (stopping) {
      break;
    }
    if (claimed) {
      await processCommand(claimed, { db, baileys, rateLimiter, breaker, link }).catch(
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
