/** Owns the one pairing attempt that may be running, and reports its progress
 * through the status row the UI polls (whatsapp_agent_status.pairing_*).
 *
 * The UI never talks to this process: it enqueues `link_start` / `link_cancel`
 * commands in the outbox, the claim loop hands them here, and this reports
 * back by updating the status row. So "start linking" returns immediately and
 * the claim loop is never blocked while a human finds their phone.
 *
 * Each attempt links into its own brand-new session folder. If it succeeds,
 * that folder simply becomes the live one (see SessionStore); if it doesn't,
 * it is tidied away. Tidying is strictly best-effort and is never allowed to
 * fail the flow: a socket that has just closed may still be writing into its
 * folder, and deleting it then is how the first version of this lost a
 * freshly linked session.
 */
import { PairingSession, PairingEvent, SocketFactory, Timers } from "./pairing";
import { Account, phoneFromJid, removeDirQuiet } from "./sessionFiles";

export type PairingState =
  | "IDLE"
  | "STARTING"
  | "WAITING_FOR_SCAN"
  | "SCANNED"
  | "SUCCEEDED"
  | "EXPIRED"
  | "CANCELLED"
  | "FAILED";

export interface PairingProgress {
  state: PairingState;
  pairingId: string | null;
  qr: string | null;
  qrAt: Date | null;
  detail: string;
}

export interface PairingStore {
  updatePairing(progress: PairingProgress): Promise<void>;
}

export interface SessionAdopter {
  /** Switch the live connection over to the freshly linked session in `newDir`. */
  adopt(newDir: string): Promise<void>;
}

export interface LinkControllerDeps {
  store: PairingStore;
  adopter: SessionAdopter;
  openSocket: SocketFactory;
  /** A fresh, empty session folder for each attempt. */
  newSessionDir: () => string;
  /** Best-effort delete of a folder nobody needs; must not throw. */
  removeDir?: (dir: string) => void;
  /** Also draw the QR in the console, when one is attached. */
  onQr?: (qr: string) => void;
  onError?: (err: unknown, what: string) => void;
  maxDurationMs?: number;
  settleMs?: number;
  timers?: Timers;
}

interface Attempt {
  session: PairingSession;
  dir: string;
}

export class LinkController {
  private attempt: Attempt | null = null;
  private pairingId: string | null = null;
  /** Status writes and the session switch happen strictly in event order. */
  private queue: Promise<void> = Promise.resolve();

  constructor(private readonly deps: LinkControllerDeps) {}

  /** Begin (or restart) linking. Any attempt already running is replaced. */
  async start(pairingId: string): Promise<void> {
    this.abandonCurrent();
    this.pairingId = pairingId;
    await this.write("STARTING", null, "Starting…");

    const dir = this.deps.newSessionDir();
    const session: PairingSession = new PairingSession({
      authDir: dir,
      openSocket: this.deps.openSocket,
      onEvent: (event) => this.enqueue(() => this.handle(attempt, event)),
      ...(this.deps.maxDurationMs !== undefined && { maxDurationMs: this.deps.maxDurationMs }),
      ...(this.deps.settleMs !== undefined && { settleMs: this.deps.settleMs }),
      ...(this.deps.timers !== undefined && { timers: this.deps.timers }),
    });
    const attempt: Attempt = { session, dir };
    this.attempt = attempt;
    try {
      await session.start();
    } catch (err) {
      this.attempt = null;
      this.tidy(dir);
      await this.write("FAILED", null, `Couldn't start linking: ${messageOf(err)}`);
    }
  }

  /** The user pressed Cancel (or closed the dialog). */
  async cancel(): Promise<void> {
    const attempt = this.attempt;
    if (!attempt) {
      return;
    }
    attempt.session.cancel(); // reports CANCELLED through the normal event path
    await this.queue;
  }

  /** For a clean shutdown: stop the attempt without reporting anything new. */
  shutdown(): void {
    this.abandonCurrent();
  }

  /** Replace-not-report: a new attempt supersedes the old one silently. */
  private abandonCurrent(): void {
    const attempt = this.attempt;
    this.attempt = null;
    if (attempt) {
      if (!attempt.session.finished) {
        attempt.session.cancel();
      }
      this.tidy(attempt.dir);
    }
  }

  private enqueue(work: () => Promise<void>): void {
    this.queue = this.queue.then(work).catch((err: unknown) => this.deps.onError?.(err, "pairing"));
  }

  private async handle(attempt: Attempt, event: PairingEvent): Promise<void> {
    if (attempt !== this.attempt) {
      return; // a superseded attempt
    }
    switch (event.kind) {
      case "qr":
        await this.write("WAITING_FOR_SCAN", event.qr, "Scan the code with WhatsApp.");
        this.deps.onQr?.(event.qr);
        return;
      case "scanned":
        await this.write("SCANNED", null, "Scanned. Finishing the link…");
        return;
      case "linked":
        await this.finishLinking(attempt, event.account);
        return;
      default:
        this.attempt = null;
        this.tidy(attempt.dir);
        await this.write(
          event.kind === "expired" ? "EXPIRED" : event.kind === "cancelled" ? "CANCELLED" : "FAILED",
          null,
          event.detail,
        );
    }
  }

  private async finishLinking(attempt: Attempt, account: Account): Promise<void> {
    this.attempt = null;
    const who = phoneFromJid(account.jid) ?? "the new phone";
    await this.write("SCANNED", null, `Linked ${who}. Switching over…`);
    try {
      await this.deps.adopter.adopt(attempt.dir);
      await this.write("SUCCEEDED", null, `Linked ${who}.`);
    } catch (err) {
      this.deps.onError?.(err, "adopt");
      // The new session is NOT deleted on failure: it may be perfectly good, and
      // a later pass can still use or tidy it. Only the user-facing state changes.
      await this.write("FAILED", null, `Couldn't switch to the new phone: ${messageOf(err)}`);
    }
  }

  private tidy(dir: string): void {
    try {
      (this.deps.removeDir ?? removeDirQuiet)(dir);
    } catch (err) {
      this.deps.onError?.(err, "tidy");
    }
  }

  private async write(state: PairingState, qr: string | null, detail: string): Promise<void> {
    try {
      await this.deps.store.updatePairing({
        state,
        pairingId: this.pairingId,
        qr,
        qrAt: qr ? new Date() : null,
        detail,
      });
    } catch (err) {
      this.deps.onError?.(err, "updatePairing");
    }
  }
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
