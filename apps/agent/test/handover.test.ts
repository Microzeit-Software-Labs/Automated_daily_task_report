import assert from "node:assert/strict";
import { test } from "node:test";
import { CODE } from "../src/disconnect";
import { awaitOpen, HandoverDeps, performHandover } from "../src/handover";
import { closeWith, FakeSocket, FakeTimers } from "./fakes";

function harness(overrides: Partial<HandoverDeps<FakeSocket>> = {}, hasOld = true) {
  const log: string[] = [];
  const candidate = new FakeSocket();
  const old = new FakeSocket();
  const failures: unknown[] = [];
  const deps: HandoverDeps<FakeSocket> = {
    waitForQuiet: async (dir) => {
      log.push(`quiet ${dir}`);
      return true;
    },
    openSocket: async (dir) => {
      log.push(`open ${dir}`);
      return candidate;
    },
    awaitOpen: async () => {
      log.push("awaitOpen");
    },
    activate: (dir) => {
      log.push(`activate ${dir}`);
    },
    promote: () => {
      log.push("promote");
    },
    current: () => (hasOld ? old : null),
    retire: async () => {
      log.push("retire");
    },
    onRetireFailed: (err) => failures.push(err),
    ...overrides,
  };
  return { deps, log, candidate, old, failures };
}

test("the order: quiet, open, prove it connects, activate, use it, and only then retire the old one", async () => {
  const { deps, log } = harness();

  await performHandover("/new", deps);

  assert.deepEqual(log, [
    "quiet /new",
    "open /new",
    "awaitOpen",
    "activate /new",
    "promote",
    "retire",
  ]);
});

test("if the new session won't connect, the old link is untouched and keeps working", async () => {
  const { deps, log, candidate, old } = harness({
    awaitOpen: async () => {
      log.push("awaitOpen");
      throw new Error("WhatsApp didn't recognise the new session.");
    },
  });

  await assert.rejects(() => performHandover("/new", deps), /didn't recognise/);

  assert.deepEqual(log, ["quiet /new", "open /new", "awaitOpen"], "no activation, no switch, no retire");
  assert.equal(candidate.ended, true, "the failed candidate is closed");
  assert.equal(old.ended, false, "the working connection is left alone");
});

test("if activating fails, the candidate is closed and the old link is untouched", async () => {
  const { deps, log, candidate, old } = harness({
    activate: () => {
      throw new Error("could not write the pointer");
    },
  });

  await assert.rejects(() => performHandover("/new", deps), /pointer/);

  assert.ok(!log.includes("promote"));
  assert.ok(!log.includes("retire"));
  assert.equal(candidate.ended, true);
  assert.equal(old.ended, false);
});

test("if the new session can't even be opened, nothing else happens", async () => {
  const { deps, log, old } = harness({
    openSocket: async () => {
      throw new Error("no network stack");
    },
  });

  await assert.rejects(() => performHandover("/new", deps), /no network stack/);

  assert.deepEqual(log, ["quiet /new"]);
  assert.equal(old.ended, false);
});

test("failing to log the old device out doesn't undo a switch that already worked", async () => {
  const { deps, log, failures } = harness({
    retire: async () => {
      throw new Error("the old socket was already gone");
    },
  });

  await performHandover("/new", deps); // resolves

  assert.ok(log.includes("promote"));
  assert.equal(failures.length, 1, "reported, but only as a note");
});

test("with no current connection there is nothing to retire", async () => {
  const { deps, log } = harness({}, false);

  await performHandover("/new", deps);

  assert.ok(log.includes("promote"));
  assert.ok(!log.includes("retire"));
});

test("it waits for the new folder to go quiet before opening a connection on it", async () => {
  const order: string[] = [];
  const { deps } = harness({
    waitForQuiet: async () => {
      order.push("quiet-start");
      await new Promise<void>((resolve) => setImmediate(resolve));
      order.push("quiet-done");
      return true;
    },
    openSocket: async () => {
      order.push("open");
      return new FakeSocket();
    },
  });

  await performHandover("/new", deps);

  assert.deepEqual(order, ["quiet-start", "quiet-done", "open"]);
});

// -- awaitOpen ----------------------------------------------------------------------

function pending(timeoutMs = 45_000) {
  const socket = new FakeSocket();
  const timers = new FakeTimers();
  const result = awaitOpen(socket, { timeoutMs, timers });
  return { socket, timers, result };
}

test("awaitOpen resolves once the connection opens", async () => {
  const { socket, result } = pending();
  socket.emit({ connection: "connecting" });
  socket.emit({ connection: "open" });
  await result;
});

test("awaitOpen rejects if WhatsApp asks to pair again", async () => {
  const { socket, result } = pending();
  socket.emit({ qr: "some-qr" });
  await assert.rejects(result, /didn't recognise/);
});

test("awaitOpen rejects if the session is logged out straight away", async () => {
  const { socket, result } = pending();
  socket.emit(closeWith(CODE.loggedOut));
  await assert.rejects(result, /logged the new session out/);
});

test("awaitOpen rejects on any other close, naming the code", async () => {
  const { socket, result } = pending();
  socket.emit(closeWith(CODE.connectionClosed));
  await assert.rejects(result, /code 428/);
});

test("awaitOpen rejects if nothing happens in time", async () => {
  const { timers, result } = pending(45_000);
  timers.fire(45_000);
  await assert.rejects(result, /in time/);
});

test("awaitOpen stops listening and clears its timer once settled", async () => {
  const { socket, timers, result } = pending();
  socket.emit({ connection: "open" });
  await result;
  assert.equal(socket.listeners.length, 0);
  assert.equal(timers.count, 0);
});
