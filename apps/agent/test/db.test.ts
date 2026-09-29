import assert from "node:assert/strict";
import { test } from "node:test";
import { AgentDb } from "../src/db";

// pg's Pool is lazy -- constructing one never opens a connection -- so this
// runs without a database. What it does exercise is AgentDb's own path to
// agent_queries.sql, resolved from the *compiled* location (dist/src/). A
// wrong relative path there crashes the agent on startup, and nothing else
// in this suite constructs an AgentDb, so without this test that class of
// bug only surfaces on a real first run.
test("AgentDb finds and loads agent_queries.sql from its compiled location", async () => {
  const db = new AgentDb("postgresql://nobody:nothing@127.0.0.1:1/none");
  await db.close();
});

test("AgentDb loaded every statement it relies on", async () => {
  const db = new AgentDb("postgresql://nobody:nothing@127.0.0.1:1/none");
  const queries = (db as unknown as { queries: Record<string, string> }).queries;
  for (const name of [
    "claim_next",
    "complete",
    "release_transient",
    "reset_stale_claims",
    "set_wa_message_id",
    "upsert_status",
  ]) {
    assert.ok(queries[name], `missing statement ${name}`);
  }
  await db.close();
});
