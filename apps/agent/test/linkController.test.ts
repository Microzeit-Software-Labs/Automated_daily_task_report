import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";
import { CODE } from "../src/disconnect";
import { LinkController, PairingProgress } from "../src/linkController";
import { closeWith, FakeTimers, flush, socketFactory } from "./fakes";

const ACCOUNT = { id: "919876543210:12@s.whatsapp.net", name: "Kaif" };

let root: string;
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "interlock-link-"));
});
afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

function setup(
  options: {
    adopt?: (dir: string) => Promise<void>;
    removeDir?: (dir: string) => void;
  } = {},
) {
  const writes: PairingProgress[] = [];
  const adopted: string[] = [];
  const errors: string[] = [];
  const qrs: string[] = [];
  const dirs: string[] = [];
  const factory = socketFactory();
  const timers = new FakeTimers();
  const controller = new LinkController({
    store: { updatePairing: async (p) => void writes.push(p) },
    adopter: {
      adopt: async (dir) => {
        adopted.push(dir);
        await (options.adopt ?? (async () => undefined))(dir);
      },
    },
    openSocket: factory.openSocket,
    // A fresh folder per attempt, created the way Baileys would (with a creds file).
    newSessionDir: () => {
      const dir = join(root, `s-${dirs.length + 1}`);
      mkdirSync(dir, { recursive: true });
      writeFileSync(join(dir, "creds.json"), "{}");
      dirs.push(dir);
      return dir;
    },
    ...(options.removeDir && { removeDir: options.removeDir }),
    settleMs: 0,
    timers,
    onQr: (qr) => qrs.push(qr),
    onError: (_err, what) => errors.push(what),
  });
  const states = () => writes.map((w) => w.state);
  return { controller, writes, adopted, errors, qrs, factory, dirs, states, timers };
}

test("starting reports STARTING, then the QR for the UI to draw", async () => {
  const { controller, factory, writes, states, qrs } = setup();

  await controller.start("pairing-1");
  factory.sockets[0]!.emit({ qr: "QR-1" });
  await flush();

  assert.deepEqual(states(), ["STARTING", "WAITING_FOR_SCAN"]);
  const waiting = writes.at(-1)!;
  assert.equal(waiting.pairingId, "pairing-1");
  assert.equal(waiting.qr, "QR-1");
  assert.ok(waiting.qrAt instanceof Date);
  assert.deepEqual(qrs, ["QR-1"]);
});

test("each attempt links into its own new folder", async () => {
  const { controller, factory, dirs } = setup();

  await controller.start("one");
  await controller.start("two");

  assert.equal(dirs.length, 2);
  assert.notEqual(dirs[0], dirs[1]);
  assert.deepEqual(factory.dirs, dirs, "the socket for each attempt used its own folder");
});

test("a completed scan switches over, then reports success, and keeps the new session", async () => {
  const order: string[] = [];
  const { controller, factory, adopted, dirs, states, timers } = setup({
    adopt: async () => {
      order.push("adopted");
    },
  });
  await controller.start("p");
  const sock = factory.sockets[0]!;
  sock.emit({ qr: "QR" });
  sock.emit({ isNewLogin: true });
  sock.emit(closeWith(CODE.restartRequired));
  await flush();
  const second = factory.sockets[1]!;
  second.user = ACCOUNT;
  second.emit({ connection: "open" });
  timers.fire(0); // the short settle delay after the link opens
  await flush();

  assert.deepEqual(adopted, [dirs[0]], "adopts the session linked in this attempt's folder");
  assert.deepEqual(order, ["adopted"]);
  assert.deepEqual(states(), ["STARTING", "WAITING_FOR_SCAN", "SCANNED", "SCANNED", "SUCCEEDED"]);
  assert.equal(existsSync(dirs[0]!), true, "the freshly linked session is never deleted");
});

test("the success message names the new number", async () => {
  const { controller, factory, writes, timers } = setup();
  await controller.start("p");
  factory.sockets[0]!.user = ACCOUNT;
  factory.sockets[0]!.emit({ connection: "open" });
  timers.fire(0);
  await flush();

  assert.equal(writes.at(-1)!.state, "SUCCEEDED");
  assert.match(writes.at(-1)!.detail, /\+919876543210/);
});

test("if switching over fails it says so, and does NOT delete the new session", async () => {
  // The bug this guards: a failed switch used to delete the new session's
  // credentials, losing a link WhatsApp had already accepted.
  const { controller, factory, writes, errors, dirs, timers } = setup({
    adopt: async () => {
      throw new Error("the new session didn't connect in time");
    },
  });
  await controller.start("p");
  factory.sockets[0]!.user = ACCOUNT;
  factory.sockets[0]!.emit({ connection: "open" });
  timers.fire(0);
  await flush();

  assert.equal(writes.at(-1)!.state, "FAILED");
  assert.match(writes.at(-1)!.detail, /didn't connect in time/);
  assert.deepEqual(errors, ["adopt"]);
  assert.equal(existsSync(join(dirs[0]!, "creds.json")), true, "credentials are still there");
});

test("tidying a folder Windows won't release never breaks the flow", async () => {
  const { controller, factory, states, errors } = setup({
    removeDir: () => {
      throw new Error("EBUSY: resource busy or locked");
    },
  });
  await controller.start("p");
  factory.sockets[0]!.emit({ qr: "QR" });

  await controller.cancel();

  assert.equal(states().at(-1), "CANCELLED", "still reported as cancelled");
  assert.deepEqual(errors, ["tidy"], "noted, not fatal");
});

test("a successful link is not affected by a failing clean-up of an earlier attempt", async () => {
  const { controller, factory, states, timers } = setup({
    removeDir: () => {
      throw new Error("ENOTEMPTY: directory not empty");
    },
  });
  await controller.start("first");
  factory.sockets[0]!.emit({ qr: "QR" });
  await controller.start("second"); // supersedes; tidying the first one fails
  factory.sockets[1]!.user = ACCOUNT;
  factory.sockets[1]!.emit({ connection: "open" });
  timers.fire(0);
  await flush();

  assert.equal(states().at(-1), "SUCCEEDED");
});

test("cancelling reports CANCELLED and tidies away the attempt's folder", async () => {
  const { controller, factory, dirs, states } = setup();
  await controller.start("p");
  factory.sockets[0]!.emit({ qr: "QR" });

  await controller.cancel();

  assert.equal(states().at(-1), "CANCELLED");
  assert.equal(existsSync(dirs[0]!), false);
  assert.equal(factory.sockets[0]!.ended, true);
});

test("cancelling with nothing running does nothing", async () => {
  const { controller, writes } = setup();
  await controller.cancel();
  assert.equal(writes.length, 0);
});

test("a QR nobody scans ends as EXPIRED", async () => {
  const { controller, factory, states, dirs } = setup();
  await controller.start("p");
  factory.sockets[0]!.emit({ qr: "QR" });
  factory.sockets[0]!.emit(closeWith(CODE.timedOut));
  await flush();
  assert.equal(states().at(-1), "EXPIRED");
  assert.equal(existsSync(dirs[0]!), false, "the unused folder is tidied away");
});

test("starting again replaces the running attempt; the old one can no longer report", async () => {
  const { controller, factory, writes, states } = setup();
  await controller.start("first");
  const old = factory.sockets[0]!;
  old.emit({ qr: "OLD-QR" });
  await flush();

  await controller.start("second");
  old.emit({ qr: "OLD-QR-2" }); // a late event from the superseded attempt
  old.emit(closeWith(CODE.loggedOut));
  factory.sockets[1]!.emit({ qr: "NEW-QR" });
  await flush();

  assert.equal(old.ended, true, "the old attempt's socket is closed");
  assert.equal(writes.filter((w) => w.qr === "OLD-QR-2").length, 0);
  assert.equal(writes.at(-1)!.pairingId, "second");
  assert.equal(writes.at(-1)!.qr, "NEW-QR");
  assert.deepEqual(states().slice(-2), ["STARTING", "WAITING_FOR_SCAN"]);
});

test("a socket that won't even open ends as FAILED", async () => {
  const writes: PairingProgress[] = [];
  const controller = new LinkController({
    store: { updatePairing: async (p) => void writes.push(p) },
    adopter: { adopt: async () => undefined },
    openSocket: async () => {
      throw new Error("no network stack");
    },
    newSessionDir: () => join(root, "s-x"),
    timers: new FakeTimers(),
  });

  await controller.start("p");

  assert.equal(writes.at(-1)!.state, "FAILED");
  assert.match(writes.at(-1)!.detail, /no network stack/);
});

test("shutting down abandons the attempt without reporting a new state", async () => {
  const { controller, factory, writes } = setup();
  await controller.start("p");
  factory.sockets[0]!.emit({ qr: "QR" });
  await flush();
  const before = writes.length;

  controller.shutdown();
  await flush();

  assert.equal(factory.sockets[0]!.ended, true);
  assert.equal(writes.length, before);
});
