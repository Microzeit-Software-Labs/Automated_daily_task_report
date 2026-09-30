import assert from "node:assert/strict";
import { test } from "node:test";
import { CODE } from "../src/disconnect";
import { PairingEvent, PairingSession } from "../src/pairing";
import { closeWith, FakeTimers, flush, socketFactory } from "./fakes";

const ACCOUNT = { id: "919876543210:12@s.whatsapp.net", name: "Kaif" };

function setup(overrides: { maxRestarts?: number; maxDurationMs?: number } = {}) {
  const events: PairingEvent[] = [];
  const timers = new FakeTimers();
  const factory = socketFactory();
  const session = new PairingSession({
    authDir: "/tmp/pairing",
    openSocket: factory.openSocket,
    onEvent: (event) => events.push(event),
    settleMs: 10,
    timers,
    ...overrides,
  });
  const kinds = () => events.map((e) => e.kind);
  return { session, events, timers, factory, kinds };
}

test("shows each QR code as WhatsApp rotates it", async () => {
  const { session, factory, events } = setup();
  await session.start();

  factory.sockets[0]!.emit({ qr: "QR-1" });
  factory.sockets[0]!.emit({ qr: "QR-2" });

  assert.deepEqual(events, [
    { kind: "qr", qr: "QR-1" },
    { kind: "qr", qr: "QR-2" },
  ]);
  assert.equal(session.finished, false);
});

test("scan, restart, open: links the phone and closes the socket", async () => {
  const { session, factory, events, timers, kinds } = setup();
  await session.start();
  const first = factory.sockets[0]!;

  first.emit({ qr: "QR-1" });
  first.emit({ isNewLogin: true }); // the phone scanned
  first.emit(closeWith(CODE.restartRequired)); // WhatsApp asks for a fresh socket
  await flush();

  assert.equal(factory.sockets.length, 2, "a fresh socket on the same credentials");
  assert.deepEqual(factory.dirs, ["/tmp/pairing", "/tmp/pairing"]);

  const second = factory.sockets[1]!;
  second.user = ACCOUNT;
  second.emit({ connection: "open" });
  assert.equal(session.finished, false, "it waits a moment for Baileys to finish its key uploads");
  timers.fire(10);

  assert.deepEqual(kinds(), ["qr", "scanned", "linked"]);
  assert.deepEqual(events.at(-1), {
    kind: "linked",
    account: { jid: ACCOUNT.id, name: "Kaif" },
  });
  assert.equal(second.ended, true);
  assert.equal(session.finished, true);
});

test("events from the socket it has moved past are ignored", async () => {
  const { session, factory, kinds } = setup();
  await session.start();
  const first = factory.sockets[0]!;
  first.emit({ isNewLogin: true });
  first.emit(closeWith(CODE.restartRequired));
  await flush();

  first.emit({ qr: "stale" });
  first.emit(closeWith(CODE.loggedOut));

  assert.deepEqual(kinds(), ["scanned"]);
  assert.equal(session.finished, false);
});

test("a QR that nobody scans expires", async () => {
  const { session, factory, events } = setup();
  await session.start();
  factory.sockets[0]!.emit({ qr: "QR-1" });

  factory.sockets[0]!.emit(closeWith(CODE.timedOut));

  assert.equal(events.at(-1)?.kind, "expired");
  assert.equal(factory.sockets[0]!.ended, true);
});

test("taking too long overall expires it, too", async () => {
  const { session, factory, events, timers } = setup({ maxDurationMs: 180_000 });
  await session.start();
  factory.sockets[0]!.emit({ qr: "QR-1" });

  timers.fire(180_000);

  assert.equal(events.at(-1)?.kind, "expired");
  assert.equal(session.finished, true);
  assert.equal(factory.sockets[0]!.ended, true);
});

test("WhatsApp rejecting the link fails it", async () => {
  const { session, factory, events } = setup();
  await session.start();
  factory.sockets[0]!.emit(closeWith(CODE.loggedOut));
  const last = events.at(-1);
  assert.equal(last?.kind, "failed");
  assert.match(last?.kind === "failed" ? last.detail : "", /rejected/);
  assert.equal(session.finished, true);
});

test("no internet before the scan says so", async () => {
  const { session, factory, events } = setup();
  await session.start();
  factory.sockets[0]!.emit(closeWith(CODE.connectionClosed));
  const last = events.at(-1);
  assert.equal(last?.kind, "failed");
  assert.match(last?.kind === "failed" ? last.detail : "", /internet/);
});

test("dropping after the scan but before finishing is a failure, not a hang", async () => {
  const { session, factory, events } = setup();
  await session.start();
  factory.sockets[0]!.emit({ isNewLogin: true });
  factory.sockets[0]!.emit(closeWith(CODE.connectionClosed));
  const last = events.at(-1);
  assert.equal(last?.kind, "failed");
  assert.match(last?.kind === "failed" ? last.detail : "", /didn't finish/);
});

test("it gives up after too many restarts", async () => {
  const { session, factory, events } = setup({ maxRestarts: 2 });
  await session.start();
  for (let i = 0; i < 3; i++) {
    factory.sockets.at(-1)!.emit(closeWith(CODE.restartRequired));
    await flush();
  }
  assert.equal(factory.sockets.length, 3, "two restarts allowed");
  assert.equal(events.at(-1)?.kind, "failed");
});

test("cancelling ends the socket and reports it once", async () => {
  const { session, factory, events, timers } = setup();
  await session.start();

  session.cancel();
  session.cancel();

  assert.deepEqual(events.map((e) => e.kind), ["cancelled"]);
  assert.equal(factory.sockets[0]!.ended, true);
  assert.equal(timers.count, 0, "no timer is left running");
});

test("cancelling while the socket is still opening doesn't leak it", async () => {
  const events: PairingEvent[] = [];
  const opened: Array<{ ended: boolean }> = [];
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const session = new PairingSession({
    authDir: "/tmp/pairing",
    timers: new FakeTimers(),
    onEvent: (event) => events.push(event),
    openSocket: async () => {
      await gate;
      const socket = socketFactory();
      const s = await socket.openSocket("/tmp/pairing");
      opened.push(s);
      return s;
    },
  });

  const starting = session.start();
  session.cancel();
  release();
  await starting;

  assert.equal(opened[0]!.ended, true);
  assert.deepEqual(events.map((e) => e.kind), ["cancelled"]);
});

test("a link that opens without saying who it is fails rather than guessing", async () => {
  const { session, factory, events } = setup();
  await session.start();
  factory.sockets[0]!.emit({ connection: "open" }); // no user set
  assert.equal(events.at(-1)?.kind, "failed");
});
