/** What to do when WhatsApp closes the connection.
 *
 * Pure: a status code in, a decision out, so every branch is unit-tested and
 * the socket code in baileys.ts stays a thin switch. The codes are
 * Baileys' `DisconnectReason` values, kept as plain numbers because that enum
 * lives in an ESM-only package this CommonJS project only loads dynamically.
 *
 * The principle: *never loop*. A dropped connection is retried with back-off;
 * anything that retrying cannot fix (logged out, invalid session, taken over
 * by another session, account refused) stops and tells the UI, because
 * hammering WhatsApp with doomed reconnects is exactly what gets a number
 * flagged.
 */

export const CODE = {
  loggedOut: 401,
  forbidden: 403,
  timedOut: 408,
  multideviceMismatch: 411,
  connectionClosed: 428,
  connectionReplaced: 440,
  badSession: 500,
  unavailableService: 503,
  restartRequired: 515,
} as const;

/** Why the link is not usable, in a form the UI can switch on. */
export type StateReason =
  | "NOT_LINKED"
  | "LOGGED_OUT"
  | "SESSION_INVALID"
  | "REPLACED"
  | "FORBIDDEN";

export type DisconnectAction =
  /** Normal right after a pairing or login: open a fresh socket straight away. */
  | { kind: "restart_now" }
  /** A dropped connection (network, WhatsApp server): try again after a delay. */
  | { kind: "reconnect"; delayMs: number }
  /** The saved session is dead. Needs a new QR scan; retrying cannot help. */
  | { kind: "needs_login"; reason: "LOGGED_OUT" | "SESSION_INVALID"; detail: string }
  /** Something else owns the session or the account. Stop, and say so. */
  | { kind: "halt"; reason: "REPLACED" | "FORBIDDEN"; detail: string };

/** The HTTP-ish status code Baileys attaches to a disconnect error (a Boom). */
export function statusCodeOf(error: unknown): number | undefined {
  const code = (error as { output?: { statusCode?: unknown } } | null | undefined)?.output
    ?.statusCode;
  return typeof code === "number" ? code : undefined;
}

export const MIN_RECONNECT_DELAY_MS = 2_000;
export const MAX_RECONNECT_DELAY_MS = 60_000;

/** 2 s, 4 s, 8 s, 16 s, 32 s, then 60 s for ever. `attempt` starts at 0. */
export function reconnectDelayMs(attempt: number): number {
  const exponent = Math.max(0, Math.min(attempt, 10));
  return Math.min(MIN_RECONNECT_DELAY_MS * 2 ** exponent, MAX_RECONNECT_DELAY_MS);
}

export function classifyDisconnect(statusCode: number | undefined, attempt: number): DisconnectAction {
  switch (statusCode) {
    case CODE.restartRequired:
      return { kind: "restart_now" };
    case CODE.loggedOut:
      return {
        kind: "needs_login",
        reason: "LOGGED_OUT",
        detail: "WhatsApp logged this device out (for example from Linked Devices on the phone).",
      };
    case CODE.badSession:
    case CODE.multideviceMismatch:
      return {
        kind: "needs_login",
        reason: "SESSION_INVALID",
        detail: "The saved WhatsApp session is no longer valid.",
      };
    case CODE.connectionReplaced:
      return {
        kind: "halt",
        reason: "REPLACED",
        detail: "Another session took over this WhatsApp link, so this one stopped.",
      };
    case CODE.forbidden:
      return {
        kind: "halt",
        reason: "FORBIDDEN",
        detail: "WhatsApp refused this account. Check WhatsApp on the phone.",
      };
    default:
      // 408 / 428 / 503 / no code at all: the connection dropped, not the session.
      return { kind: "reconnect", delayMs: reconnectDelayMs(attempt) };
  }
}
