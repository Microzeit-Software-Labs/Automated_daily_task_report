/** The WhatsApp session(s) on disk: which one is live, who it belongs to, and
 * how to move to a new one without ever renaming a folder.
 *
 * Why no renames: a freshly linked session is written to by Baileys for a
 * while after the link completes (hundreds of key and sync files), and on
 * Windows a folder with files in active use, or being scanned, cannot be
 * renamed or deleted (EPERM / ENOTEMPTY). The first version of "change phone"
 * renamed the new session folder into place, hit exactly that, and its
 * clean-up then deleted the new credentials. So instead:
 *
 *   .wa-session/                the original single folder, used until the
 *                               first time a phone is linked from the UI
 *   .wa-sessions/s-<time>-<id>/ one folder per linked phone, never renamed
 *   .wa-sessions/active.json    a tiny pointer naming the live folder
 *
 * "Switching" is writing the pointer (an atomic file replace). Finished-with
 * folders are deleted only as best-effort tidying, never on the path that
 * decides whether the switch worked.
 *
 * Plain synchronous fs calls throughout: these run on rare, human-triggered
 * paths, and simple and sequential is easier to reason about, and to test
 * against real temp folders.
 */
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, join, resolve } from "node:path";

export interface Account {
  jid: string;
  name: string | null;
}

interface CredsFile {
  me?: { id?: string; name?: string };
}

function readCreds(authDir: string): CredsFile | null {
  try {
    return JSON.parse(readFileSync(join(authDir, "creds.json"), "utf-8")) as CredsFile;
  } catch {
    return null;
  }
}

/** Linked = the pairing actually completed (Baileys records `me` only then).
 * A half-finished pairing leaves a creds.json without it. */
export function isLinked(authDir: string | null): boolean {
  return authDir !== null && Boolean(readCreds(authDir)?.me?.id);
}

export function readAccount(authDir: string | null): Account | null {
  const me = authDir === null ? undefined : readCreds(authDir)?.me;
  return me?.id ? { jid: me.id, name: me.name ?? null } : null;
}

/** "919876543210:12@s.whatsapp.net" -> "+919876543210". */
export function phoneFromJid(jid: string | null | undefined): string | null {
  const match = /^(\d+)(?::\d+)?@/.exec(jid ?? "");
  return match ? `+${match[1]}` : null;
}

function stamp(at: Date): string {
  return at.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
}

export type Rename = (from: string, to: string) => void;

/** Windows can hold a file briefly (antivirus, indexer); retry a few times.
 * Only ever used on single files (the pointer), never on session folders. */
export function renameWithRetry(from: string, to: string): void {
  let lastError: unknown;
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      renameSync(from, to);
      return;
    } catch (err) {
      lastError = err;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 100);
    }
  }
  throw lastError;
}

/** Delete a folder if Windows lets us; say whether it did. Never throws:
 * a folder that is still busy is simply tidied on a later pass. */
export function removeDirQuiet(dir: string): boolean {
  try {
    rmSync(dir, { recursive: true, force: true });
    return !existsSync(dir);
  } catch {
    return false;
  }
}

const GENERATION = /^s-\d{8}T\d{6}Z-[0-9a-f]{4}$/;

export class SessionStore {
  readonly legacyDir: string;
  readonly sessionsDir: string;
  private readonly pointerFile: string;

  /** `baseDir` is apps/agent (never under dist/: a clean rebuild must not wipe a pairing). */
  constructor(baseDir: string) {
    this.legacyDir = join(baseDir, ".wa-session");
    this.sessionsDir = join(baseDir, ".wa-sessions");
    this.pointerFile = join(this.sessionsDir, "active.json");
  }

  /** The folder the live connection uses, or null when no session is active
   * (never linked, or retired after a logout). */
  activeDir(): string | null {
    const pointed = this.readPointer();
    if (pointed === undefined) {
      return existsSync(this.legacyDir) ? this.legacyDir : null;
    }
    if (pointed === "unreadable") {
      // Don't lose a good session to a damaged pointer: newest complete one wins.
      const newest = this.generations().filter(isLinked).at(-1);
      return newest ?? (isLinked(this.legacyDir) ? this.legacyDir : null);
    }
    if (pointed === null) {
      return null;
    }
    const dir = join(this.sessionsDir, pointed);
    return existsSync(dir) ? dir : null;
  }

  /** A fresh folder path for linking a phone. Not created: Baileys creates it. */
  newGenerationDir(at: Date = new Date(), randomHex: string = randomHex4()): string {
    mkdirSync(this.sessionsDir, { recursive: true });
    return join(this.sessionsDir, `s-${stamp(at)}-${randomHex}`);
  }

  /** Make `dir` the live session. Refuses an incomplete one, and anything that
   * isn't one of this store's own folders. Atomic: the pointer is replaced in
   * one step, so a crash leaves either the old value or the new, never a mix. */
  activate(dir: string): void {
    if (resolve(dirname(dir)) !== resolve(this.sessionsDir) || !GENERATION.test(basename(dir))) {
      throw new Error("That is not a session folder this agent created.");
    }
    if (!isLinked(dir)) {
      throw new Error("The new WhatsApp session is not complete; keeping the current one.");
    }
    this.writePointer(basename(dir));
  }

  /** The live session is dead (logged out, invalid): stop using it. The folder
   * stays on disk, untouched, until it is tidied up later. Returns it. */
  retireActive(): string | null {
    const dir = this.activeDir();
    this.writePointer(null);
    return dir;
  }

  /** Every numbered session folder, oldest first (the name sorts by time). */
  generations(): string[] {
    if (!existsSync(this.sessionsDir)) {
      return [];
    }
    return readdirSync(this.sessionsDir, { withFileTypes: true })
      .filter((e) => e.isDirectory() && GENERATION.test(e.name))
      .map((e) => join(this.sessionsDir, e.name))
      .sort();
  }

  /** Tidy up: delete old session folders, keeping the live one and the newest
   * `keepRetired` others (the original `.wa-session/` counts as the oldest).
   * Best-effort in every way: a folder that won't delete is skipped and
   * retried next time; nothing here can throw or block a switch. */
  prune(keepRetired = 1, remove: (dir: string) => boolean = removeDirQuiet): string[] {
    const active = this.activeDir();
    const candidates = [
      ...(existsSync(this.legacyDir) ? [this.legacyDir] : []),
      ...this.generations(),
    ].filter((dir) => dir !== active);
    const doomed = candidates.slice(0, Math.max(0, candidates.length - keepRetired));
    return doomed.filter((dir) => {
      try {
        return remove(dir);
      } catch {
        return false;
      }
    });
  }

  private readPointer(): string | null | undefined | "unreadable" {
    if (!existsSync(this.pointerFile)) {
      return undefined;
    }
    try {
      const parsed = JSON.parse(readFileSync(this.pointerFile, "utf-8")) as { dir?: unknown };
      if (parsed.dir === null) {
        return null;
      }
      return typeof parsed.dir === "string" && GENERATION.test(parsed.dir)
        ? parsed.dir
        : "unreadable";
    } catch {
      return "unreadable";
    }
  }

  private writePointer(dirName: string | null): void {
    mkdirSync(this.sessionsDir, { recursive: true });
    const temp = `${this.pointerFile}.tmp`;
    writeFileSync(temp, JSON.stringify({ dir: dirName }));
    renameWithRetry(temp, this.pointerFile);
  }
}

function randomHex4(): string {
  return Math.floor(Math.random() * 0x10000)
    .toString(16)
    .padStart(4, "0");
}

export interface QuietOptions {
  /** How long nothing may change before the folder counts as quiet. */
  stableMs?: number;
  /** Give up waiting after this long and carry on. */
  maxMs?: number;
  pollMs?: number;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

/** A cheap fingerprint of a flat folder: how many files, and the newest change. */
export function folderSignature(dir: string): string {
  let newest = 0;
  let count = 0;
  let bytes = 0;
  try {
    for (const name of readdirSync(dir)) {
      try {
        const info = statSync(join(dir, name));
        count += 1;
        bytes += info.size;
        newest = Math.max(newest, info.mtimeMs);
      } catch {
        // vanished between listing and stat: still a change worth noticing next poll
      }
    }
  } catch {
    return "missing";
  }
  return `${count}:${bytes}:${newest}`;
}

/** Wait until nothing in `dir` has changed for `stableMs`.
 *
 * A socket that has just been closed can still be flushing the last of its
 * key and sync files. Opening a second connection on the folder, or deleting
 * it, while that goes on is how a good session gets damaged. Resolves `true`
 * once quiet, `false` if it never settled within `maxMs`. */
export async function waitForQuiet(dir: string, options: QuietOptions = {}): Promise<boolean> {
  const stableMs = options.stableMs ?? 2_000;
  const maxMs = options.maxMs ?? 30_000;
  const pollMs = options.pollMs ?? 250;
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));

  const startedAt = now();
  let last = folderSignature(dir);
  let unchangedSince = now();
  for (;;) {
    await sleep(pollMs);
    const current = folderSignature(dir);
    if (current !== last) {
      last = current;
      unchangedSince = now();
    } else if (now() - unchangedSince >= stableMs) {
      return true;
    }
    if (now() - startedAt >= maxMs) {
      return false;
    }
  }
}
