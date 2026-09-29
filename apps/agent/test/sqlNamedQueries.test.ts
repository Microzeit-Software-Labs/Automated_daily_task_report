import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { loadNamedQueries, toPositionalQuery } from "../src/sqlNamedQueries";

const REAL_SQL_PATH = join(__dirname, "..", "..", "sql", "agent_queries.sql");

const SAMPLE = `
-- Some header comment, ignored.
-- name: claim_next
SELECT * FROM t WHERE status = 'PENDING' AND expires_at > now() + (:margin_seconds || ' seconds')::interval;

-- name: complete
UPDATE t SET status = 'DONE', result = CAST(:result AS jsonb) WHERE id = :id;

-- name: reset_stale_claims
UPDATE t SET status = 'PENDING' WHERE status = 'CLAIMED';
`;

test("loadNamedQueries splits the file into its named blocks", () => {
  const queries = loadNamedQueries(SAMPLE);
  assert.equal(Object.keys(queries).length, 3);
  assert.match(queries["claim_next"] as string, /SELECT \* FROM t/);
  assert.match(queries["complete"] as string, /UPDATE t SET status = 'DONE'/);
});

test("loadNamedQueries ignores text before the first header", () => {
  const queries = loadNamedQueries(SAMPLE);
  for (const sql of Object.values(queries)) {
    assert.doesNotMatch(sql, /Some header comment/);
  }
});

test("loadNamedQueries trims each block", () => {
  const queries = loadNamedQueries(SAMPLE);
  const sql = queries["reset_stale_claims"] as string;
  assert.equal(sql, sql.trim());
});

test("toPositionalQuery converts a single named parameter", () => {
  const { text, values } = toPositionalQuery("SELECT * FROM t WHERE id = :id", { id: "abc" });
  assert.equal(text, "SELECT * FROM t WHERE id = $1");
  assert.deepEqual(values, ["abc"]);
});

test("toPositionalQuery reuses one positional slot for a repeated name", () => {
  const { text, values } = toPositionalQuery("SELECT :id, :id, :other", {
    id: "x",
    other: "y",
  });
  assert.equal(text, "SELECT $1, $1, $2");
  assert.deepEqual(values, ["x", "y"]);
});

test("toPositionalQuery does not confuse a name with an adjacent cast", () => {
  // The whole reason the shared SQL file uses CAST(...) instead of "::jsonb"
  // directly after a placeholder -- SQLAlchemy's own text() parser cannot
  // reliably split ":result" from an immediately adjacent "::" cast. This
  // parser handles both correctly regardless, but the file itself must stay
  // written the SQLAlchemy-safe way since Python is the other consumer.
  const { text, values } = toPositionalQuery("CAST(:result AS jsonb)", { result: '{"a":1}' });
  assert.equal(text, "CAST($1 AS jsonb)");
  assert.deepEqual(values, ['{"a":1}']);
});

test("toPositionalQuery raises on a parameter the caller forgot to pass", () => {
  assert.throws(() => toPositionalQuery("SELECT :missing", {}), /missing/);
});

test("the real agent_queries.sql has exactly the six statements this agent uses", () => {
  const queries = loadNamedQueries(readFileSync(REAL_SQL_PATH, "utf-8"));
  const names = Object.keys(queries).sort();
  assert.deepEqual(names, [
    "claim_next",
    "complete",
    "release_transient",
    "reset_stale_claims",
    "set_wa_message_id",
    "upsert_status",
  ]);
  for (const sql of Object.values(queries)) {
    assert.ok(sql.length > 0);
  }
});
