import assert from "node:assert/strict";
import { test } from "node:test";
import { RateLimiter } from "../src/rateLimiter";

/** A fake clock plus a fake sleep that just advances it, so waitForTurn's
 * delay is provable without a real timer. */
function makeClock(startAt = 0): { now: () => number; sleep: (ms: number) => Promise<void> } {
  let current = startAt;
  return {
    now: () => current,
    sleep: async (ms: number) => {
      current += ms;
    },
  };
}

test("the first call never waits", async () => {
  const clock = makeClock();
  const limiter = new RateLimiter(3000, 200, clock.now, clock.sleep);
  assert.equal(limiter.msUntilNextTurn(), 0);
  await limiter.waitForTurn();
  assert.equal(clock.now(), 0);
});

test("a second call within the minimum interval waits out the remainder", async () => {
  const clock = makeClock();
  const limiter = new RateLimiter(3000, 200, clock.now, clock.sleep);

  await limiter.waitForTurn();
  await clock.sleep(1000); // pretend 1s of real work happened between calls
  assert.equal(limiter.msUntilNextTurn(), 2000);
  await limiter.waitForTurn();

  assert.equal(clock.now(), 3000); // 0 -> 1000 (work) -> waited 2000 -> turn taken at 3000
});

test("a call after the interval has already elapsed does not wait", async () => {
  const clock = makeClock();
  const limiter = new RateLimiter(3000, 200, clock.now, clock.sleep);

  await limiter.waitForTurn();
  await clock.sleep(5000);
  assert.equal(limiter.msUntilNextTurn(), 0);
});

test("canSendNow is true under the hourly cap", () => {
  const clock = makeClock();
  const limiter = new RateLimiter(0, 3, clock.now);
  limiter.recordSend();
  limiter.recordSend();
  assert.equal(limiter.canSendNow(), true);
});

test("canSendNow is false once the hourly cap is reached", () => {
  const clock = makeClock();
  const limiter = new RateLimiter(0, 2, clock.now);
  limiter.recordSend();
  limiter.recordSend();
  assert.equal(limiter.canSendNow(), false);
});

test("canSendNow forgets sends older than an hour", async () => {
  const clock = makeClock();
  const limiter = new RateLimiter(0, 1, clock.now);
  limiter.recordSend();
  assert.equal(limiter.canSendNow(), false);

  await clock.sleep(3_600_001);
  assert.equal(limiter.canSendNow(), true);
});
