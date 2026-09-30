import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";
import {
  folderSignature,
  isLinked,
  phoneFromJid,
  readAccount,
  SessionStore,
  waitForQuiet,
} from "../src/sessionFiles";

let root: string;
let store: SessionStore;
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "interlock-session-"));
  store = new SessionStore(root);
});
afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

const PHONE_A = { id: "919876543210:12@s.whatsapp.net", name: "Kaif" };
const PHONE_B = { id: "911234567890:3@s.whatsapp.net", name: "Other" };

/** A session folder on disk. `me: null` is a half-finished pairing. */
function writeSession(dir: string, me: { id: string; name?: string } | null, marker = "x"): string {
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "creds.json"), JSON.stringify(me ? { me } : {}));
  writeFileSync(join(dir, "marker.txt"), marker);
  return dir;
}
const marker = (dir: string) => readFileSync(join(dir, "marker.txt"), "utf-8");
const gen = (day: string, id = "abcd") => store.newGenerationDir(new Date(`2026-10-${day}T10:00:00Z`), id);

test("a session is linked only once pairing recorded who it is", () => {
  assert.equal(isLinked(writeSession(join(root, "done"), PHONE_A)), true);
  assert.equal(isLinked(writeSession(join(root, "half"), null)), false, "half-finished pairing");
  assert.equal(isLinked(join(root, "missing")), false);
  assert.equal(isLinked(null), false);

  const corrupt = join(root, "corrupt");
  mkdirSync(corrupt);
  writeFileSync(join(corrupt, "creds.json"), "{not json");
  assert.equal(isLinked(corrupt), false);
});

test("readAccount reports the linked account", () => {
  assert.deepEqual(readAccount(writeSession(join(root, "a"), PHONE_A)), {
    jid: PHONE_A.id,
    name: "Kaif",
  });
  assert.equal(readAccount(writeSession(join(root, "b"), null)), null);
  assert.equal(readAccount(null), null);
});

test("phoneFromJid makes a phone number out of a WhatsApp id", () => {
  assert.equal(phoneFromJid("919876543210:12@s.whatsapp.net"), "+919876543210");
  assert.equal(phoneFromJid("919876543210@s.whatsapp.net"), "+919876543210");
  assert.equal(phoneFromJid("not-a-jid"), null);
  assert.equal(phoneFromJid(null), null);
});

// -- which session is live ------------------------------------------------------

test("with no pointer, the original folder is the live session", () => {
  assert.equal(store.activeDir(), null, "nothing yet");
  writeSession(store.legacyDir, PHONE_A);
  assert.equal(store.activeDir(), store.legacyDir);
});

test("activating a new session makes it live and leaves the original untouched", () => {
  writeSession(store.legacyDir, PHONE_A, "original");
  const fresh = writeSession(gen("01"), PHONE_B, "fresh");

  store.activate(fresh);

  assert.equal(store.activeDir(), fresh);
  assert.equal(marker(store.legacyDir), "original", "no folder was renamed or removed");
  assert.equal(existsSync(`${store.sessionsDir}/active.json.tmp`), false, "no temp file left");
});

test("switching never renames or moves a session folder", () => {
  // The bug this design exists to prevent: a rename of a folder Baileys is still
  // writing into fails on Windows. Everything is addressed in place.
  const first = writeSession(gen("01", "0001"), PHONE_A, "first");
  const second = writeSession(gen("02", "0002"), PHONE_B, "second");
  store.activate(first);
  store.activate(second);
  store.activate(first);

  assert.deepEqual(store.generations(), [first, second]);
  assert.equal(marker(first), "first");
  assert.equal(marker(second), "second");
});

test("a half-finished session is refused and the live one is unchanged", () => {
  const good = writeSession(gen("01", "0001"), PHONE_A);
  store.activate(good);
  const half = writeSession(gen("02", "0002"), null);

  assert.throws(() => store.activate(half), /not complete/);

  assert.equal(store.activeDir(), good);
});

test("only this agent's own session folders can be activated", () => {
  const elsewhere = writeSession(join(root, "elsewhere"), PHONE_A);
  assert.throws(() => store.activate(elsewhere), /not a session folder/);
  const misnamed = writeSession(join(store.sessionsDir, "not-a-generation"), PHONE_A);
  assert.throws(() => store.activate(misnamed), /not a session folder/);
});

test("retiring stops using the session but deletes nothing", () => {
  const live = writeSession(gen("01"), PHONE_A, "keep me");
  store.activate(live);

  const retired = store.retireActive();

  assert.equal(retired, live);
  assert.equal(store.activeDir(), null);
  assert.equal(marker(live), "keep me");
});

test("a retired original folder is not brought back by the missing pointer", () => {
  writeSession(store.legacyDir, PHONE_A);
  assert.equal(store.retireActive(), store.legacyDir);
  assert.equal(store.activeDir(), null, "the dead session is not reused");
  assert.equal(existsSync(store.legacyDir), true, "and it was not deleted");
});

test("a pointer to a folder that no longer exists means no live session", () => {
  const live = writeSession(gen("01"), PHONE_A);
  store.activate(live);
  rmSync(live, { recursive: true });
  assert.equal(store.activeDir(), null);
});

test("a damaged pointer doesn't lose the newest complete session", () => {
  writeSession(gen("01", "0001"), PHONE_A);
  const newer = writeSession(gen("02", "0002"), PHONE_B);
  writeSession(gen("03", "0003"), null); // newest, but never finished linking
  writeFileSync(join(store.sessionsDir, "active.json"), "{ truncated");

  assert.equal(store.activeDir(), newer);
});

test("a damaged pointer falls back to the original folder when nothing else is complete", () => {
  writeSession(store.legacyDir, PHONE_A);
  mkdirSync(store.sessionsDir, { recursive: true });
  writeFileSync(join(store.sessionsDir, "active.json"), "nonsense");
  assert.equal(store.activeDir(), store.legacyDir);
});

test("new session folders have unique names that sort by time", () => {
  const earlier = store.newGenerationDir(new Date("2026-10-01T10:00:00Z"), "ffff");
  const later = store.newGenerationDir(new Date("2026-10-01T10:00:01Z"), "0000");
  assert.notEqual(earlier, later);
  assert.ok(earlier < later);
  assert.equal(existsSync(earlier), false, "Baileys creates the folder, not the store");
});

// -- tidying up ------------------------------------------------------------------

test("tidying keeps the live session and the newest other one", () => {
  const g1 = writeSession(gen("01", "0001"), PHONE_A);
  const g2 = writeSession(gen("02", "0002"), PHONE_A);
  const g3 = writeSession(gen("03", "0003"), PHONE_B);
  store.activate(g3);

  const removed = store.prune();

  assert.deepEqual(removed, [g1]);
  assert.deepEqual(store.generations(), [g2, g3]);
});

test("the original folder counts as the oldest when tidying", () => {
  writeSession(store.legacyDir, PHONE_A);
  const g1 = writeSession(gen("01", "0001"), PHONE_A);
  const g2 = writeSession(gen("02", "0002"), PHONE_B);
  store.activate(g2);

  const removed = store.prune();

  assert.deepEqual(removed, [store.legacyDir]);
  assert.equal(existsSync(g1), true);
  assert.equal(existsSync(g2), true);
});

test("tidying a folder Windows won't release never throws and never blocks anything", () => {
  const g1 = writeSession(gen("01", "0001"), PHONE_A);
  const g2 = writeSession(gen("02", "0002"), PHONE_A);
  const g3 = writeSession(gen("03", "0003"), PHONE_B);
  store.activate(g3);

  const removed = store.prune(0, (dir) => {
    if (dir === g1) {
      throw new Error("EBUSY: resource busy or locked");
    }
    rmSync(dir, { recursive: true });
    return true;
  });

  assert.deepEqual(removed, [g2], "only what really went is reported");
  assert.equal(existsSync(g1), true, "the locked one is left for next time");
  assert.equal(store.activeDir(), g3, "and the live session is untouched");
});

test("tidying with nothing old does nothing", () => {
  writeSession(store.legacyDir, PHONE_A);
  assert.deepEqual(store.prune(), []);
  assert.equal(existsSync(store.legacyDir), true, "the only session is never removed");
});

// -- waiting for a folder to go quiet -----------------------------------------------

/** A clock that only moves when the code under test sleeps. */
function fakeClock(onTick: (t: number) => void = () => undefined) {
  let t = 0;
  return {
    now: () => t,
    sleep: async (ms: number) => {
      t += ms;
      onTick(t);
    },
    time: () => t,
  };
}

test("folderSignature changes when a file is added or rewritten", () => {
  const dir = writeSession(join(root, "s"), PHONE_A);
  const before = folderSignature(dir);
  writeFileSync(join(dir, "extra.json"), "{}");
  const afterAdd = folderSignature(dir);
  assert.notEqual(before, afterAdd);
  writeFileSync(join(dir, "extra.json"), '{"a":1}');
  assert.notEqual(afterAdd, folderSignature(dir));
  assert.equal(folderSignature(join(root, "gone")), "missing");
});

test("an already-quiet folder is reported quiet after the stable period", async () => {
  const dir = writeSession(join(root, "s"), PHONE_A);
  const clock = fakeClock();

  const quiet = await waitForQuiet(dir, { stableMs: 2000, pollMs: 250, ...clock });

  assert.equal(quiet, true);
  assert.ok(clock.time() >= 2000 && clock.time() < 3000, `waited ${clock.time()} ms`);
});

test("a folder still being written to is waited out, not swapped under", async () => {
  const dir = writeSession(join(root, "s"), PHONE_A);
  let writes = 0;
  // Something keeps writing for the first 3 seconds (a socket flushing keys).
  const clock = fakeClock((t) => {
    if (t <= 3000) {
      writeFileSync(join(dir, `key-${++writes}.json`), "{}");
    }
  });

  const quiet = await waitForQuiet(dir, { stableMs: 2000, pollMs: 250, maxMs: 30_000, ...clock });

  assert.equal(quiet, true);
  assert.ok(writes > 5, "it really was being written to");
  assert.ok(clock.time() >= 3000 + 2000, `only calm after the writing stopped (${clock.time()} ms)`);
});

test("a folder that never settles is given up on after the maximum wait", async () => {
  const dir = writeSession(join(root, "s"), PHONE_A);
  let n = 0;
  const clock = fakeClock(() => {
    writeFileSync(join(dir, `key-${++n}.json`), "{}");
  });

  const quiet = await waitForQuiet(dir, { stableMs: 2000, pollMs: 250, maxMs: 10_000, ...clock });

  assert.equal(quiet, false);
  assert.ok(clock.time() >= 10_000 && clock.time() < 11_000);
  assert.ok(readdirSync(dir).length > 10);
});
