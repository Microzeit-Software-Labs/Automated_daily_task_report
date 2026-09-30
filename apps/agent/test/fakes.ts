// Test doubles for the pairing code: a socket that emits whatever events a
// test tells it to, and timers that only fire when a test says so.
import type { PairingSocket, PairingUpdate, Timers } from "../src/pairing";

export class FakeSocket implements PairingSocket {
  listeners: Array<(update: PairingUpdate) => void> = [];
  ended = false;
  user: { id: string; name?: string } | undefined;

  ev = {
    on: (_event: "connection.update", listener: (update: PairingUpdate) => void): void => {
      this.listeners.push(listener);
    },
    off: (_event: "connection.update", listener: (update: PairingUpdate) => void): void => {
      this.listeners = this.listeners.filter((l) => l !== listener);
    },
  };

  end(_error: Error | undefined): void {
    this.ended = true;
  }

  emit(update: PairingUpdate): void {
    for (const listener of this.listeners) {
      listener(update);
    }
  }
}

/** What Baileys emits when a connection closes with a status code. */
export function closeWith(code: number | undefined): PairingUpdate {
  return {
    connection: "close",
    lastDisconnect: { error: code === undefined ? new Error("boom") : { output: { statusCode: code } } },
  };
}

/** A factory that hands out fresh FakeSockets and remembers them. */
export function socketFactory(): {
  sockets: FakeSocket[];
  openSocket: (authDir: string) => Promise<FakeSocket>;
  dirs: string[];
} {
  const sockets: FakeSocket[] = [];
  const dirs: string[] = [];
  return {
    sockets,
    dirs,
    openSocket: async (authDir: string) => {
      const socket = new FakeSocket();
      sockets.push(socket);
      dirs.push(authDir);
      return socket;
    },
  };
}

export class FakeTimers implements Timers {
  private next = 1;
  private pending = new Map<number, { callback: () => void; ms: number }>();

  set(callback: () => void, ms: number): unknown {
    const id = this.next++;
    this.pending.set(id, { callback, ms });
    return id;
  }

  clear(handle: unknown): void {
    this.pending.delete(handle as number);
  }

  get count(): number {
    return this.pending.size;
  }

  /** Run every pending timer whose delay is at most `upToMs`, oldest first. */
  fire(upToMs = Number.POSITIVE_INFINITY): void {
    for (const [id, { callback, ms }] of [...this.pending]) {
      if (ms <= upToMs && this.pending.delete(id)) {
        callback();
      }
    }
  }
}

/** Let queued promise callbacks run (status writes, the session switch). */
export async function flush(): Promise<void> {
  for (let i = 0; i < 10; i++) {
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
}
