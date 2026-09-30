import assert from "node:assert/strict";
import { test } from "node:test";
import {
  classifyDisconnect,
  CODE,
  MAX_RECONNECT_DELAY_MS,
  reconnectDelayMs,
  statusCodeOf,
} from "../src/disconnect";

test("reconnect back-off doubles from 2 s and stops at 60 s", () => {
  const delays = [0, 1, 2, 3, 4, 5, 6, 20].map(reconnectDelayMs);
  assert.deepEqual(delays, [2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000]);
  assert.equal(reconnectDelayMs(1_000_000), MAX_RECONNECT_DELAY_MS);
  assert.equal(reconnectDelayMs(-3), 2000);
});

test("a dropped connection is retried with back-off", () => {
  for (const code of [CODE.timedOut, CODE.connectionClosed, CODE.unavailableService, undefined]) {
    assert.deepEqual(classifyDisconnect(code, 2), { kind: "reconnect", delayMs: 8000 }, String(code));
  }
});

test("515 after a scan means open a fresh socket straight away", () => {
  assert.deepEqual(classifyDisconnect(CODE.restartRequired, 4), { kind: "restart_now" });
});

test("being logged out needs a new QR scan, and is never retried", () => {
  const action = classifyDisconnect(CODE.loggedOut, 0);
  assert.equal(action.kind, "needs_login");
  assert.equal(action.kind === "needs_login" && action.reason, "LOGGED_OUT");
});

test("a bad or mismatched session needs a new QR scan", () => {
  for (const code of [CODE.badSession, CODE.multideviceMismatch]) {
    const action = classifyDisconnect(code, 0);
    assert.equal(action.kind, "needs_login");
    assert.equal(action.kind === "needs_login" && action.reason, "SESSION_INVALID");
  }
});

test("another session taking over, or a refused account, halts instead of fighting", () => {
  const replaced = classifyDisconnect(CODE.connectionReplaced, 0);
  assert.equal(replaced.kind, "halt");
  assert.equal(replaced.kind === "halt" && replaced.reason, "REPLACED");

  const forbidden = classifyDisconnect(CODE.forbidden, 0);
  assert.equal(forbidden.kind, "halt");
  assert.equal(forbidden.kind === "halt" && forbidden.reason, "FORBIDDEN");
});

test("statusCodeOf reads a Boom-style error and tolerates anything else", () => {
  assert.equal(statusCodeOf({ output: { statusCode: 401 } }), 401);
  assert.equal(statusCodeOf(new Error("x")), undefined);
  assert.equal(statusCodeOf(undefined), undefined);
  assert.equal(statusCodeOf({ output: { statusCode: "401" } }), undefined);
});
