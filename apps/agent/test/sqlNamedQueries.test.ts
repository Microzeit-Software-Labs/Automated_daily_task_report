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

test("toPositionalQuery never reads a :: cast as a parameter", () => {
  // The first real run of the agent failed every poll with
  // 'missing parameter "interval"' because of exactly this.
  const { text, values } = toPositionalQuery(
    "SELECT now() + (:margin_seconds || ' seconds')::interval, :result::jsonb",
    { margin_seconds: 2, result: "{}" }
  );
  assert.equal(text, "SELECT now() + ($1 || ' seconds')::interval, $2::jsonb");
  assert.deepEqual(values, [2, "{}"]);
});

test("loadNamedQueries drops comment lines inside a block", () => {
  const queries = loadNamedQueries(
    "-- name: q\n-- mentions :result and \"::jsonb\" in prose\nSELECT :id;\n  -- indented note\n"
  );
  assert.equal(queries["q"], "SELECT :id;");
});

// Exactly the parameters db.ts passes for each statement. Converting every
// real statement with them is what would have caught the "::interval" bug
// before the agent ever ran.
const DB_TS_PARAMS: Record<string, Record<string, unknown>> = {
  claim_next: { claimed_by: "w", margin_seconds: 2 },
  claim_next_control: { claimed_by: "w", margin_seconds: 2 },
  update_pairing: {
    pairing_state: "WAITING_FOR_SCAN",
    pairing_id: "p",
    pairing_qr: "qr",
    pairing_qr_at: null,
    pairing_detail: "",
  },
  complete: { id: "c", result: "{}" },
  release_transient: { id: "c" },
  reset_stale_claims: {},
  set_wa_message_id: { id: "c", wa_message_id: "m" },
  upsert_status: {
    state: "CONNECTED",
    detail: "",
    status_reason: null,
    account_jid: null,
    account_name: null,
    agent_version: "0.1.0",
    last_successful_send_at: null,
    last_canary_at: null,
    last_canary_ok: null,
  },
};

test("every real statement converts with exactly the parameters db.ts passes", () => {
  const queries = loadNamedQueries(readFileSync(REAL_SQL_PATH, "utf-8"));
  for (const [name, sql] of Object.entries(queries)) {
    const params = DB_TS_PARAMS[name];
    assert.ok(params, `no parameter fixture for "${name}"`);
    const { text, values } = toPositionalQuery(sql, params);
    assert.doesNotMatch(text, /(?<!:):[a-z_]+/, `"${name}" still has an unbound :name`);
    assert.doesNotMatch(text, /^\s*--/m, `"${name}" still carries comment lines`);
    assert.equal(values.length, Object.keys(params).length, `"${name}" uses every parameter once`);
  }
});

test("toPositionalQuery raises on a parameter the caller forgot to pass", () => {
  assert.throws(() => toPositionalQuery("SELECT :missing", {}), /missing/);
});

test("the real agent_queries.sql has exactly the statements this agent uses", () => {
  const queries = loadNamedQueries(readFileSync(REAL_SQL_PATH, "utf-8"));
  const names = Object.keys(queries).sort();
  assert.deepEqual(names, [
    "claim_next",
    "claim_next_control",
    "complete",
    "release_transient",
    "reset_stale_claims",
    "set_wa_message_id",
    "update_pairing",
    "upsert_status",
  ]);
  for (const sql of Object.values(queries)) {
    assert.ok(sql.length > 0);
  }
});
