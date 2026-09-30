/** One attempt to link a phone: show a QR, wait for the scan, finish.
 *
 * Runs in its *own* temporary auth directory and its own socket, never
 * touching the live session -- so scheduled reports keep sending while a
 * user is part-way through linking a new phone, and abandoning, cancelling or
 * failing the attempt changes nothing. Only after the new phone has fully
 * linked does the caller swap it in (see LinkController / BaileysAgent.adopt).
 *
 * The socket is injected (`openSocket`), so every branch below is
 * unit-tested with a fake that emits the events Baileys would.
 */
import { CODE, statusCodeOf } from "./disconnect";
import type { Account } from "./sessionFiles";

/** The slice of a Baileys socket a pairing uses. */
export interface PairingUpdate {
  connection?: "open" | "connecting" | "close";
  qr?: string;
  isNewLogin?: boolean;
  receivedPendingNotifications?: boolean;
  lastDisconnect?: { error?: unknown };
}

export interface PairingSocket {
  ev: { on(event: "connection.update", listener: (update: PairingUpdate) => void): void };
  user?: { id: string; name?: string } | undefined;
  end(error: Error | undefined): void;
}

export type SocketFactory = (authDir: string) => Promise<PairingSocket>;

export type PairingEvent =
  | { kind: "qr"; qr: string }
  | { kind: "scanned" }
  | { kind: "linked"; account: Account }
  | { kind: "expired" | "failed" | "cancelled"; detail: string };

export interface Timers {
  set(callback: () => void, ms: number): unknown;
  clear(handle: unknown): void;
}

const realTimers: Timers = {
  // unref'd: a pairing's deadline must never be the thing keeping a process
  // alive (the agent stays up for its own reasons).
  set: (callback, ms) => setTimeout(callback, ms).unref(),
  clear: (handle) => clearTimeout(handle as NodeJS.Timeout),
};

export interface PairingOptions {
  authDir: string;
  openSocket: SocketFactory;
  onEvent: (event: PairingEvent) => void;
  /** Give up if the phone hasn't finished linking by then. */
  maxDurationMs?: number;
  /** After linking, let Baileys finish writing keys before the socket is closed. */
  settleMs?: number;
  /** A fresh socket is needed right after the scan; allow a few. */
  maxRestarts?: number;
  timers?: Timers;
}

export class PairingSession {
  private socket: PairingSocket | null = null;
  private done = false;
  private scanned = false;
  private restarts = 0;
  private deadline: unknown;
  private settle: unknown;
  private readonly timers: Timers;

  constructor(private readonly opts: PairingOptions) {
    this.timers = opts.timers ?? realTimers;
  }

  get finished(): boolean {
    return this.done;
  }

  /** Opens the socket. Resolves once it exists -- not when the phone links. */
  async start(): Promise<void> {
    this.deadline = this.timers.set(
      () =>
        this.finish({
          kind: "expired",
          detail: "The QR code wasn't scanned in time.",
        }),
      this.opts.maxDurationMs ?? 180_000,
    );
    await this.attach();
  }

  cancel(): void {
    this.finish({ kind: "cancelled", detail: "Linking was cancelled." });
  }

  private async attach(): Promise<void> {
    const socket = await this.opts.openSocket(this.opts.authDir);
    if (this.done) {
      socket.end(undefined); // cancelled while the socket was still opening
      return;
    }
    this.socket = socket;
    socket.ev.on("connection.update", (update) => this.onUpdate(socket, update));
  }

  private onUpdate(socket: PairingSocket, update: PairingUpdate): void {
    if (this.done || socket !== this.socket) {
      return; // an event from a socket this session has already moved past
    }
    if (update.qr) {
      this.opts.onEvent({ kind: "qr", qr: update.qr });
    }
    if (update.isNewLogin) {
      this.scanned = true;
      this.opts.onEvent({ kind: "scanned" });
    }
    if (update.connection === "open") {
      this.onOpen(socket);
    } else if (update.connection === "close") {
      this.onClose(statusCodeOf(update.lastDisconnect?.error));
    }
  }

  private onOpen(socket: PairingSocket): void {
    const user = socket.user;
    if (!user?.id) {
      this.finish({ kind: "failed", detail: "WhatsApp connected but didn't say who this is." });
      return;
    }
    const account: Account = { jid: user.id, name: user.name ?? null };
    // Closing the instant the link opens can cut off the key uploads Baileys
    // does right after login; give it a moment first.
    this.settle = this.timers.set(
      () => this.finish({ kind: "linked", account }),
      this.opts.settleMs ?? 3_000,
    );
  }

  private onClose(code: number | undefined): void {
    if (this.settle !== undefined) {
      return; // it had opened; closing it is part of finishing
    }
    if (code === CODE.restartRequired) {
      // Normal right after a scan: WhatsApp wants a fresh socket on the new credentials.
      if (this.restarts >= (this.opts.maxRestarts ?? 3)) {
        this.finish({ kind: "failed", detail: "Linking kept restarting. Please try again." });
        return;
      }
      this.restarts += 1;
      this.attach().catch((err: unknown) =>
        this.finish({ kind: "failed", detail: `Couldn't continue linking: ${messageOf(err)}` }),
      );
      return;
    }
    if (code === CODE.timedOut && !this.scanned) {
      this.finish({ kind: "expired", detail: "The QR code expired before it was scanned." });
    } else if (code === CODE.loggedOut) {
      this.finish({ kind: "failed", detail: "WhatsApp rejected the link. Please try again." });
    } else if (this.scanned) {
      this.finish({ kind: "failed", detail: "Linking didn't finish. Please try again." });
    } else {
      this.finish({
        kind: "failed",
        detail: "Couldn't reach WhatsApp. Check the internet connection and try again.",
      });
    }
  }

  private finish(event: PairingEvent): void {
    if (this.done) {
      return;
    }
    this.done = true;
    this.timers.clear(this.deadline);
    this.timers.clear(this.settle);
    try {
      this.socket?.end(undefined);
    } catch {
      // already closed
    }
    this.opts.onEvent(event);
  }
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
