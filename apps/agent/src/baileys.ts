/** The real WhatsApp connection: a linked device via Baileys.
 *
 * Cannot be exercised by any automated test in this repository -- the same
 * accepted gap GoogleSheetsProvider has for the real Google Sheets API (see
 * HANDOVER.md). Verified only by the manual walkthrough in
 * docs/whatsapp-agent-setup.md, against a real (ideally dedicated) number.
 *
 * @whiskeysockets/baileys ships as ESM-only, while the rest of this project
 * is CommonJS (pg, pino and dotenv all still are). Rather than converting
 * the whole project to ESM for one dependency, only the runtime values this
 * file actually calls (makeWASocket, DisconnectReason, useMultiFileAuthState)
 * are loaded via a dynamic import(), cached after the first call; every type
 * reference below is a type-only import, which TypeScript erases entirely
 * and so never produces a require() call in the compiled output.
 */
import type { Boom } from "@hapi/boom";
import type {
  ConnectionState as BaileysConnectionState,
  proto,
  WASocket,
} from "@whiskeysockets/baileys" with { "resolution-mode": "import" };
import { join } from "node:path";
import qrcodeTerminal from "qrcode-terminal";
import { AgentDb } from "./db";
import { logger } from "./logger";

type BaileysModule = typeof import("@whiskeysockets/baileys", {
  with: { "resolution-mode": "import" },
});

let baileysModulePromise: Promise<BaileysModule> | null = null;
function loadBaileys(): Promise<BaileysModule> {
  baileysModulePromise ??= import("@whiskeysockets/baileys");
  return baileysModulePromise;
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
  authDir?: string;
  onStateChange?: (state: AgentConnectionState, detail: string) => void;
}

export class BaileysAgent {
  private sock!: WASocket;
  private loggedOutCode!: number;
  private state: AgentConnectionState = "CONNECTING";
  private closing = false;
  private readonly authDir: string;
  private readonly db: AgentDb;
  private readonly onStateChangeCb: BaileysAgentOptions["onStateChange"];
  private readonly pendingSends = new Map<string, PendingSend>();

  private constructor(options: BaileysAgentOptions) {
    this.db = options.db;
    // dist/src/ -> apps/agent/. Must not live under dist/: a clean rebuild
    // would silently wipe the pairing and force a new QR scan.
    this.authDir = options.authDir ?? join(__dirname, "..", "..", ".wa-session");
    this.onStateChangeCb = options.onStateChange;
  }

  static async connect(options: BaileysAgentOptions): Promise<BaileysAgent> {
    const agent = new BaileysAgent(options);
    await agent.open();
    return agent;
  }

  private async open(): Promise<void> {
    const { default: makeWASocket, DisconnectReason, useMultiFileAuthState } = await loadBaileys();
    this.loggedOutCode = DisconnectReason.loggedOut;

    const { state: authState, saveCreds } = await useMultiFileAuthState(this.authDir);
    this.sock = makeWASocket({ auth: authState });
    this.sock.ev.on("creds.update", saveCreds);
    this.sock.ev.on("connection.update", (update) => this.handleConnectionUpdate(update));
    this.sock.ev.on("messages.update", (updates) => this.handleMessageUpdates(updates));
    // Groups never get a per-message status in messages.update -- Baileys
    // reports their delivery per member here instead (see handleReceipt in
    // Baileys' messages-recv.js). The first one proves delivery to a device.
    this.sock.ev.on("message-receipt.update", (receipts) => {
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
      logger.info("agent.qr_ready -- scan with WhatsApp > Linked Devices > Link a Device");
      qrcodeTerminal.generate(qr, { small: true });
    }

    if (connection === "open") {
      this.setState("CONNECTED", "connected");
    } else if (connection === "close") {
      if (this.closing) {
        return; // our own shutdown -- never reconnect (and print a new QR) on the way out
      }
      const statusCode = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
      if (statusCode === this.loggedOutCode) {
        // Deliberately no reconnect: retrying a logged-out session just
        // loops failed pairing attempts. A human must re-run the QR flow.
        this.setState("LOGIN_REQUIRED", "logged out -- run the pairing flow again");
        logger.warn("agent.logged_out -- not reconnecting automatically");
        return;
      }
      this.setState("CONNECTING", `disconnected (code ${statusCode ?? "unknown"}), reconnecting`);
      logger.warn({ statusCode }, "agent.disconnected");
      void this.open(); // rebinds this.sock in place -- existing references to `this` stay valid
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

  private setState(state: AgentConnectionState, detail: string): void {
    this.state = state;
    this.onStateChangeCb?.(state, detail);
  }

  /** Close the socket for good. Keeps the saved pairing, so the next start
   * reconnects without a QR scan. */
  close(): void {
    this.closing = true;
    this.sock.end(undefined);
  }

  currentState(): AgentConnectionState {
    return this.state;
  }

  ownJid(): string | null {
    return this.sock.user?.id ?? null;
  }

  async fetchGroups(): Promise<GroupInfo[]> {
    const groups = await this.sock.groupFetchAllParticipating();
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
    this.pendingSends.set(waMessageId, { commandId });
    try {
      const content = image ? { image, caption: body } : { text: body };
      await this.sock.sendMessage(jid, content, { messageId: waMessageId });
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
