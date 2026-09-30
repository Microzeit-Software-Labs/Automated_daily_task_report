/** Moving the live connection from the current phone to a newly linked one.
 *
 * The order is the whole point, and it was learnt the hard way: the first
 * version logged the old device out *before* it had switched to the new
 * session, so when the switch hit a Windows file lock the user was left with
 * neither. Now nothing about the current link is touched until the new one
 * has proven it works:
 *
 *   1. wait for the freshly linked session folder to go quiet (the socket
 *      that linked it may still be flushing files),
 *   2. open the new session and wait for WhatsApp to accept it,
 *   3. only then make it the active session (a pointer write),
 *   4. start using it,
 *   5. and last, best-effort, log the old device out and close it.
 *
 * Fail at 1-3 and the old connection is exactly as it was, still working.
 * Everything here is injected, so each of those promises is unit-tested with
 * fakes rather than hoped for.
 */
import { CODE, statusCodeOf } from "./disconnect";
import type { PairingUpdate } from "./pairing";

/** The slice of a socket the handover needs. */
export interface HandoverSocket {
  ev: {
    on(event: "connection.update", listener: (update: PairingUpdate) => void): void;
    off(event: "connection.update", listener: (update: PairingUpdate) => void): void;
  };
  end(error: Error | undefined): void;
}

/** All the orchestration needs to do to a socket it is done with. */
export interface Closable {
  end(error: Error | undefined): void;
}

export interface HandoverDeps<S extends Closable> {
  /** Resolves when the folder has stopped changing (or gave up waiting). */
  waitForQuiet(dir: string): Promise<boolean>;
  openSocket(dir: string): Promise<S>;
  /** Resolves when the socket is connected; rejects if WhatsApp refuses it. */
  awaitOpen(socket: S): Promise<void>;
  /** Make `dir` the live session (throws if it can't, changing nothing). */
  activate(dir: string): void;
  /** Start using `socket` as the live connection. */
  promote(socket: S): void;
  /** The connection being replaced, if there is one. */
  current(): S | null;
  /** Log the old device out of WhatsApp and close it. May fail: it's only tidying. */
  retire(old: S): Promise<void>;
  onRetireFailed?(err: unknown): void;
}

/** Everything succeeded except possibly retiring the old device. */
export async function performHandover<S extends Closable>(
  newDir: string,
  deps: HandoverDeps<S>,
): Promise<void> {
  await deps.waitForQuiet(newDir);

  const candidate = await deps.openSocket(newDir);
  try {
    await deps.awaitOpen(candidate);
    deps.activate(newDir);
  } catch (err) {
    closeQuietly(candidate);
    throw err; // nothing about the current link was touched
  }

  const old = deps.current();
  deps.promote(candidate);
  if (old) {
    try {
      await deps.retire(old);
    } catch (err) {
      deps.onRetireFailed?.(err);
    }
  }
}

function closeQuietly(socket: Closable): void {
  try {
    socket.end(undefined);
  } catch {
    // already closed
  }
}

export interface AwaitOpenOptions {
  timeoutMs?: number;
  timers?: {
    set(callback: () => void, ms: number): unknown;
    clear(handle: unknown): void;
  };
}

/** Wait for `socket` to connect. Rejects if WhatsApp asks to pair again (the
 * session isn't recognised), closes the connection, or simply never answers. */
export function awaitOpen(socket: HandoverSocket, options: AwaitOpenOptions = {}): Promise<void> {
  const timers = options.timers ?? {
    set: (callback: () => void, ms: number) => setTimeout(callback, ms),
    clear: (handle: unknown) => clearTimeout(handle as NodeJS.Timeout),
  };
  return new Promise<void>((resolve, reject) => {
    const settle = (finish: () => void) => {
      timers.clear(timer);
      socket.ev.off("connection.update", listener);
      finish();
    };
    const listener = (update: PairingUpdate): void => {
      if (update.qr) {
        settle(() => reject(new Error("WhatsApp didn't recognise the new session.")));
      } else if (update.connection === "open") {
        settle(resolve);
      } else if (update.connection === "close") {
        const code = statusCodeOf(update.lastDisconnect?.error);
        settle(() =>
          reject(
            new Error(
              code === CODE.loggedOut
                ? "WhatsApp logged the new session out straight away."
                : `The new session couldn't connect (code ${code ?? "unknown"}).`,
            ),
          ),
        );
      }
    };
    const timer = timers.set(
      () => settle(() => reject(new Error("The new session didn't connect in time."))),
      options.timeoutMs ?? 45_000,
    );
    socket.ev.on("connection.update", listener);
  });
}
