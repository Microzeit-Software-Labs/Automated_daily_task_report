import assert from "node:assert/strict";
import { test } from "node:test";
import { CircuitBreaker } from "../src/circuitBreaker";

test("starts closed", () => {
  assert.equal(new CircuitBreaker(5).isOpen(), false);
});

test("opens once the threshold of consecutive failures is reached", () => {
  const breaker = new CircuitBreaker(3);
  breaker.recordFailure();
  breaker.recordFailure();
  assert.equal(breaker.isOpen(), false);
  breaker.recordFailure();
  assert.equal(breaker.isOpen(), true);
});

test("a success resets the consecutive-failure count", () => {
  const breaker = new CircuitBreaker(3);
  breaker.recordFailure();
  breaker.recordFailure();
  breaker.recordSuccess();
  breaker.recordFailure();
  breaker.recordFailure();
  assert.equal(breaker.isOpen(), false);
  assert.equal(breaker.failureCount(), 2);
});

test("stays open until something resets it -- there is no auto-recovery", () => {
  const breaker = new CircuitBreaker(1);
  breaker.recordFailure();
  assert.equal(breaker.isOpen(), true);
  assert.equal(breaker.isOpen(), true); // repeated checks don't self-heal
});
