import assert from "node:assert/strict";
import { test } from "node:test";
import { ackToDeliveryAck } from "../src/baileys";

// Baileys' proto.WebMessageInfo.Status enum: ERROR=0, PENDING=1,
// SERVER_ACK=2, DELIVERY_ACK=3, READ=4, PLAYED=5. Verified against the
// installed package's own WAProto type definitions at implementation time.

test("maps every known Baileys ack status to the port's DeliveryAck", () => {
  assert.equal(ackToDeliveryAck(0), "ERROR");
  assert.equal(ackToDeliveryAck(1), "PENDING");
  assert.equal(ackToDeliveryAck(2), "SERVER");
  assert.equal(ackToDeliveryAck(3), "DEVICE");
  assert.equal(ackToDeliveryAck(4), "READ");
  assert.equal(ackToDeliveryAck(5), "READ"); // PLAYED collapses into READ
});

test("an unrecognized status degrades to PENDING rather than throwing", () => {
  assert.equal(ackToDeliveryAck(999), "PENDING");
});
