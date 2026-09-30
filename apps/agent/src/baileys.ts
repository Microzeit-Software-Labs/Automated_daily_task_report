/** The real WhatsApp connection: a linked device via Baileys.
 *
 * The socket itself cannot be exercised by an automated test in this
 * repository -- the same accepted gap GoogleSheetsProvider has for the real
 * Sheets API (see HANDOVER.md). Everything *around* it that decides what to
 * do is pure and tested: disconnect.ts (what each disconnect code means),
 * sessionFiles.ts (backups and the session swap), pairing.ts and
 * linkController.ts (linking a phone).
 *
 * Session lifecycle, in one place:
 *   - Not linked (no completed pairing on disk): no socket is opened at all,
 *     and so no QR code is ever generated in the background. The state is
 *     LOGIN_REQUIRED / NOT_LINKED until a person links a phone from the UI.
 *   - A dropped connection is retried with back-off (2 s ... 60 s).
 *   - A dead session (logged out, invalid) is moved to a backup and reported;
 *     it is never retried in a loop.
 *   - Switching to another phone (`adopt`) logs the old device out and swaps
 *     the new session in only after the new phone has fully linked.
 *
 * @whiskeysockets/baileys ships as ESM-only, while the rest of this project
 * is CommonJS (pg, pino and dotenv all still are). Rather than converting
 * the whole project to ESM for one dependency, only the runtime values this
 * file actually calls (makeWASocket, useMultiFileAuthState) are loaded via a
 * dynamic import(), cached after the first call; every type reference below
 * is a type-only import, which TypeScript erases entirely and so never
 * produces a require() call in the compiled output.
 */
import type {
  ConnectionState as BaileysConnectionState,
  proto,
  WASocket,
} from "@whiskeysockets/baileys" with { "resolution-mode": "import" };
import { AgentDb } from "./db";
import { classifyDisconnect, StateReason, statusCodeOf } from "./disconnect";
import { awaitOpen, HandoverSocket, performHandover } from "./handover";
import { logger } from "./logger";
import type { PairingSocket } from "./pairing";
import { Account, isLinked, readAccount, SessionStore, waitForQuiet } from "./sessionFiles";

type BaileysModule = typeof import("@whiskeysockets/baileys", {
  with: { "resolution-mode": "import" },
});

let baileysModulePromise: Promise<BaileysModule> | null = null;
function loadBaileys(): Promise<BaileysModule> {
  baileysModulePromise ??= import("@whiskeysockets/baileys");
  return baileysModulePromise;
}

/** Opens a socket on the session in `authDir`. Used for the live connection
 * and (through the PairingSession) for linking a phone in a scratch dir. */
export async function openSocket(authDir: string): Promise<WASocket> {
  const { default: makeWASocket, useMultiFileAuthState } = await loadBaileys();
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const sock = makeWASocket({ auth: state });
  sock.ev.on("creds.update", saveCreds);
  return sock;
}

/** The pairing code only needs the narrow PairingSocket view of it. */
export async function openPairingSocket(authDir: string): Promise<PairingSocket> {
  return (await openSocket(authDir)) as unknown as PairingSocket;
}

export type AgentConnectionState =
  | "CONNECTED"
  | "CONNECTING"
  | "LOGIN_REQUIRED"
  | "UNAVAILABLE"
  | "AUTOMATION_ERROR";

const ACK_MAP: Record<number, string> = {
  0: "ERROR",
  1: "PENDING",
  2: "SERVER",
  3: "DEVICE",
  4: "READ",
  5: "READ", // PLAYED
};

export function ackToDeliveryAck(status: number): string {
  return ACK_MAP[status] ?? "PENDING";
}

export interface GroupInfo {
  jid: string;
  subject: string;
  memberCount: number | null;
}

interface PendingSend {
  commandId: string;
  timer?: NodeJS.Timeout;
}

/** How long after WhatsApp accepts a send we wait for a rejection before
 * reporting it delivered to the server. A server-side rejection arrives as a
 * messages.update ERROR within this window. */
export const SERVER_ACCEPT_WINDOW_MS = 3_000;

export interface BaileysAgentOptions {
  db: AgentDb;
  /** Where the session(s) live. Never under dist/: a clean rebuild must not wipe a pairing. */
  store: SessionStore;
  onStateChange?: (
    state: AgentConnectionState,
    detail: string,
    reason: StateReason | null,
    account: Account | null,
  ) => void;
}

export class BaileysAgent {
  private sock: WASocket | null = null;
  private state: AgentConnectionState = "CONNECTING";
  private reason: StateReason | null = null;
  private closing = false;
  private opening = false;
  private attempt = 0;
  private reconnectTimer: NodeJS.Timeout | undefined;
  private readonly store: SessionStore;
  private readonly db: AgentDb;
  private readonly onStateChangeCb: BaileysAgentOptions["onStateChange"];
  private readonly pendingSends = new Map<string, PendingSend>();

  private constructor(options: BaileysAgentOptions) {
    this.db = options.db;
    this.store = options.store;
    this.onStateChangeCb = options.onStateChange;
  }

  static async connect(options: BaileysAgentOptions): Promise<BaileysAgent> {
    const agent = new BaileysAgent(options);
    await agent.open();
    return agent;
  }

  private async open(): Promise<void> {
    if (this.opening || this.closing) {
      return;
    }
    this.opening = true;
    try {
      const dir = this.store.activeDir();
      if (dir === null || !isLinked(dir)) {
        // Never open a socket without a completed pairing: WhatsApp would
        // answer with QR codes nobody is watching, for ever.
        this.setState(
          "LOGIN_REQUIRED",
          "WhatsApp isn't linked yet. Link a phone from Interlock > Settings > WhatsApp.",
          "NOT_LINKED",
        );
        logger.warn("agent.not_linked -- waiting for a phone to be linked from the UI");
        return;
      }

      this.attachAsLive(await openSocket(dir));
    } finally {
      this.opening = false;
    }
  }

  /** Make `sock` the live connection and listen to it. Anything a socket it
   * replaces says afterwards is ignored. */
  private attachAsLive(sock: WASocket): void {
    this.sock = sock;
    sock.ev.on("connection.update", (update) => {
      if (sock === this.sock) {
        this.handleConnectionUpdate(update);
      }
    });
    sock.ev.on("messages.update", (updates) => this.handleMessageUpdates(updates));
    // Groups never get a per-message status in messages.update -- Baileys
    // reports their delivery per member here instead (see handleReceipt in
    // Baileys' messages-recv.js). The first one proves delivery to a device.
    sock.ev.on("message-receipt.update", (receipts) => {
      for (const { key } of receipts) {
        if (key.id) {
          this.finish(key.id, "DEVICE");
        }
      }
    });
  }

  private handleConnectionUpdate(update: Partial<BaileysConnectionState>): void {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // A linked session never asks for a QR. If this one does, WhatsApp no
      // longer recognises it: stop now rather than print codes for ever.
      logger.warn("agent.session_invalid -- WhatsApp asked to pair again");
      this.haltForLogin(
        "SESSION_INVALID",
        "WhatsApp asked to pair again, so the saved session is no longer valid.",
      );
      return;
    }

    if (connection === "open") {
      this.attempt = 0;
      this.setState("CONNECTED", "connected", null);
      return;
    }
    if (connection !== "close" || this.closing) {
      return;
    }

    const statusCode = statusCodeOf(lastDisconnect?.error);
    const action = classifyDisconnect(statusCode, this.attempt);
    switch (action.kind) {
      case "restart_now":
        void this.open();
        return;
      case "reconnect": {
        this.attempt += 1;
        const seconds = Math.round(action.delayMs / 1000);
        this.setState(
          "CONNECTING",
          `disconnected (code ${statusCode ?? "unknown"}); reconnecting in ${seconds}s`,
          null,
        );
        logger.warn({ statusCode, attempt: this.attempt, delayMs: action.delayMs }, "agent.disconnected");
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = setTimeout(() => void this.open(), action.delayMs);
        return;
      }
      case "needs_login":
        logger.warn({ statusCode, reason: action.reason }, "agent.needs_login -- not reconnecting");
        this.haltForLogin(action.reason, action.detail);
        return;
      case "halt":
        logger.warn({ statusCode, reason: action.reason }, "agent.halted -- not reconnecting");
        this.endSocketQuietly();
        this.setState("UNAVAILABLE", action.detail, action.reason);
        return;
    }
  }

  /** The session is dead. Stop using it (the folder stays on disk until it is
   * tidied up later), and say a new link is needed. */
  private haltForLogin(reason: "LOGGED_OUT" | "SESSION_INVALID", detail: string): void {
    clearTimeout(this.reconnectTimer);
    this.endSocketQuietly();
    try {
      const retired = this.store.retireActive();
      logger.info({ retired }, "agent.session_retired");
    } catch (err) {
      logger.error({ err }, "agent.session_retire_failed");
    }
    this.setState("LOGIN_REQUIRED", detail, reason);
  }

  private endSocketQuietly(): void {
    const sock = this.sock;
    this.sock = null;
    try {
      sock?.end(undefined);
    } catch {
      // already closed
    }
  }

  /** Only SERVER/DEVICE/READ complete a row; anything below that has not
   * left WhatsApp's servers, per DeliveryAck.is_delivered's own definition
   * (see domain/ports/whatsapp.py) -- a PENDING ack leaves the row CLAIMED
   * and still tracked, never fabricating a terminal result. */
  private handleMessageUpdates(
    updates: Array<{ key: proto.IMessageKey; update: Partial<proto.IWebMessageInfo> }>
  ): void {
    for (const { key, update } of updates) {
      const waId = key.id;
      if (!waId || update.status == null) {
        continue;
      }
      const ack = ackToDeliveryAck(update.status);
      if (ack === "SERVER" || ack === "DEVICE" || ack === "READ") {
        this.finish(waId, ack);
      } else if (ack === "ERROR") {
        const pending = this.take(waId);
        if (pending) {
          logger.warn({ commandId: pending.commandId, waId }, "send.rejected_by_server");
          void this.db.releaseTransient(pending.commandId);
        }
      }
    }
  }

  /** Complete the outbox row once, whichever signal arrives first. */
  private finish(waId: string, ack: "SERVER" | "DEVICE" | "READ"): void {
    const pending = this.take(waId);
    if (!pending) {
      return;
    }
    void this.db
      .complete(pending.commandId, { accepted: true, provider_message_id: waId, ack })
      .catch((err: unknown) => logger.error({ err, commandId: pending.commandId }, "complete.failed"));
  }

  private take(waId: string): PendingSend | undefined {
    const pending = this.pendingSends.get(waId);
    if (pending) {
      clearTimeout(pending.timer);
      this.pendingSends.delete(waId);
    }
    return pending;
  }

  private setState(state: AgentConnectionState, detail: string, reason: StateReason | null): void {
    this.state = state;
    this.reason = reason;
    this.onStateChangeCb?.(state, detail, reason, this.account());
  }

  currentState(): AgentConnectionState {
    return this.state;
  }

  currentReason(): StateReason | null {
    return this.reason;
  }

  /** Who is linked: the live connection's account, else what the session
   * files say (so "last connected as ..." survives a disconnect). */
  account(): Account | null {
    const user = this.sock?.user;
    if (user?.id && this.state === "CONNECTED") {
      return { jid: user.id, name: user.name ?? null };
    }
    return readAccount(this.store.activeDir());
  }

  ownJid(): string | null {
    return this.sock?.user?.id ?? null;
  }

  /** "Reconnect WhatsApp" for a session that is still valid (a dropped or
   * taken-over connection). Returns what it did. */
  async reconnectNow(): Promise<"connected" | "not_linked" | "started"> {
    if (this.state === "CONNECTED") {
      return "connected";
    }
    if (!isLinked(this.store.activeDir())) {
      return "not_linked";
    }
    clearTimeout(this.reconnectTimer);
    this.attempt = 0;
    this.endSocketQuietly();
    this.setState("CONNECTING", "reconnecting", null);
    await this.open();
    return "started";
  }

  /** Switch the live connection over to the phone just linked in `newDir`.
   *
   * The current link is left completely alone until the new session has
   * proven it connects; only then is the old device logged out (see
   * handover.ts for the order and why). If anything before that point fails,
   * this throws and the current connection carries on as if nothing happened. */
  async adopt(newDir: string): Promise<void> {
    if (!isLinked(newDir)) {
      throw new Error("The new WhatsApp session is not complete; keeping the current one.");
    }
    const wasConnected = this.state === "CONNECTED";
    clearTimeout(this.reconnectTimer);
    await performHandover<WASocket>(newDir, {
      waitForQuiet: (dir) => waitForQuiet(dir),
      openSocket,
      awaitOpen: (sock) => awaitOpen(sock as unknown as HandoverSocket),
      activate: (dir) => this.store.activate(dir),
      current: () => this.sock,
      promote: (sock) => {
        this.attempt = 0;
        this.attachAsLive(sock);
        this.setState("CONNECTED", "connected", null);
      },
      retire: async (old) => {
        try {
          // Only a device that was actually connected needs logging out; one that
          // was merely reconnecting is just closed.
          if (wasConnected) {
            await old.logout();
          }
        } finally {
          try {
            old.end(undefined);
          } catch {
            // already closed
          }
        }
      },
      onRetireFailed: (err) =>
        logger.warn({ err }, "adopt.old_device_not_logged_out -- it can be removed from Linked Devices"),
    });
    logger.info("adopt.switched");
    // Tidy up old session folders. Best-effort: a locked one is left for next time.
    const removed = this.store.prune();
    if (removed.length > 0) {
      logger.info({ removed }, "adopt.old_sessions_removed");
    }
  }

  /** Close the socket for good. Keeps the saved pairing, so the next start
   * reconnects without a QR scan. */
  close(): void {
    this.closing = true;
    clearTimeout(this.reconnectTimer);
    this.endSocketQuietly();
  }

  async fetchGroups(): Promise<GroupInfo[]> {
    const sock = this.sock;
    if (!sock || this.state !== "CONNECTED") {
      throw new Error("WhatsApp is not connected");
    }
    const groups = await sock.groupFetchAllParticipating();
    return Object.values(groups).map((g) => ({
      jid: g.id,
      subject: g.subject,
      memberCount: g.participants?.length ?? null,
    }));
  }

  /** Sends, then tracks the WhatsApp message id against this outbox row.
   * The row completes on the first of: a device receipt (groups), a
   * SERVER/DEVICE/READ status (1:1 chats), or SERVER_ACCEPT_WINDOW_MS after
   * WhatsApp took the message with no rejection. A rejection in that window
   * releases the row for retry instead. */
  async send(
    jid: string,
    body: string,
    waMessageId: string,
    commandId: string,
    image: Buffer | null = null
  ): Promise<void> {
    const sock = this.sock;
    if (!sock) {
      throw new Error("WhatsApp is not connected");
    }
    this.pendingSends.set(waMessageId, { commandId });
    try {
      const content = image ? { image, caption: body } : { text: body };
      await sock.sendMessage(jid, content, { messageId: waMessageId });
    } catch (err) {
      this.take(waMessageId);
      throw err;
    }
    // WhatsApp took the message. Baileys emits no success ack for it (only an
    // ERROR on rejection, and group receipts only per member), so after a
    // short window with no rejection, record it as delivered to the server.
    const pending = this.pendingSends.get(waMessageId);
    if (pending) {
      pending.timer = setTimeout(() => this.finish(waMessageId, "SERVER"), SERVER_ACCEPT_WINDOW_MS);
    }
  }
}
