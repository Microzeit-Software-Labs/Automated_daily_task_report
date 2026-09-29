import assert from "node:assert/strict";
import { test } from "node:test";
import { rankGroups } from "../src/commands";
import type { GroupInfo } from "../src/baileys";

const SI_TEAM: GroupInfo = { jid: "si@g.us", subject: "SI Team", memberCount: 12 };
const SI_BANGALORE: GroupInfo = {
  jid: "si-blr@g.us",
  subject: "SI Team - Bangalore",
  memberCount: 4,
};
const MANAGEMENT: GroupInfo = { jid: "mgmt@g.us", subject: "Management", memberCount: 5 };

// Mirrors MockWhatsAppProvider's own test suite
// (tests/unit/test_mock_provider.py::TestGroupResolution) case for case, so
// a dev testing against the mock and production against this agent see the
// same ranking for the same input.

test("an exact match ranks highest with confidence 1.0", () => {
  const candidates = rankGroups("SI Team", [SI_TEAM, SI_BANGALORE, MANAGEMENT]);
  assert.equal(candidates[0]?.display_name, "SI Team");
  assert.equal(candidates[0]?.confidence, 1.0);
});

test("an ambiguous name returns every candidate, best first", () => {
  const candidates = rankGroups("SI Team", [SI_TEAM, SI_BANGALORE, MANAGEMENT]);
  assert.equal(candidates.length, 2);
  assert.equal(candidates[0]?.display_name, "SI Team");
  assert.equal(candidates[1]?.display_name, "SI Team - Bangalore");
});

test("no match returns an empty list", () => {
  assert.deepEqual(rankGroups("Nonexistent Group", [SI_TEAM]), []);
});

test("a blank name returns an empty list", () => {
  assert.deepEqual(rankGroups("   ", [SI_TEAM]), []);
});

test("matching is case-insensitive", () => {
  const candidates = rankGroups("si team", [SI_TEAM]);
  assert.equal(candidates[0]?.external_jid, "si@g.us");
});

test("a prefix match ranks below an exact match but above a substring match", () => {
  const prefix: GroupInfo = { jid: "p@g.us", subject: "SI Teamwork", memberCount: null };
  const substring: GroupInfo = { jid: "s@g.us", subject: "The SI Team Group", memberCount: null };
  const candidates = rankGroups("SI Team", [substring, prefix, SI_TEAM]);
  assert.deepEqual(
    candidates.map((c) => c.display_name),
    ["SI Team", "SI Teamwork", "The SI Team Group"]
  );
});

test("member_count is carried through unchanged, including null", () => {
  const withoutCount: GroupInfo = { jid: "x@g.us", subject: "SI Team", memberCount: null };
  const candidates = rankGroups("SI Team", [withoutCount]);
  assert.equal(candidates[0]?.member_count, null);
});
