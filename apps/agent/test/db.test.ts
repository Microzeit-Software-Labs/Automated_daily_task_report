import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";
import { AgentDb, ConnectionWatch } from "../src/db";

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

// --- What happens when PostgreSQL drops a connection -------------------------
//
// pg turns a dead connection into an 'error' event, and an EventEmitter with no
// listener for 'error' throws. These tests stand in a fake pool and client (still
// no database) to pin who hears about it and what the agent does next. The
// agent's one-per-database lock lives on a single connection, so losing that one
// must be reported (the agent then exits and is restarted); losing an idle or
// LISTEN connection must not be fatal.

class FakeClient extends EventEmitter {
  released: unknown[] = [];
  /** What the next query() resolves to, or rejects with. */
  next: { rows: unknown[] } | Error = { rows: [{ ok: true }] };

  async query(): Promise<{ rows: unknown[] }> {
    if (this.next instanceof Error) {
      throw this.next;
    }
    return this.next;
  }

  release(err?: unknown): void {
    this.released.push(err);
  }
}

class FakePool extends EventEmitter {
  constructor(private readonly client: FakeClient) {
    super();
  }
  async connect(): Promise<FakeClient> {
    return this.client;
  }
  async query(): Promise<{ rows: unknown[] }> {
    return { rows: [] };
  }
  async end(): Promise<void> {}
}

function watched() {
  const lost: Error[] = [];
  const trouble: Array<[Error, string]> = [];
  const watch: ConnectionWatch = {
    onLockLost: (err) => lost.push(err),
    onNonFatalError: (err, what) => trouble.push([err, what]),
  };
  const client = new FakeClient();
  const pool = new FakePool(client);
  // The fakes are not pg's classes; they have exactly the members AgentDb uses.
  const db = new AgentDb(
    "postgresql://nobody:nothing@127.0.0.1:1/none",
    watch,
    pool as unknown as ConstructorParameters<typeof AgentDb>[2]
  );
  return { db, client, pool, lost, trouble };
}

test("losing the connection that holds the lock is reported", async () => {
  const { db, client, lost } = watched();
  assert.equal(await db.acquireInstanceLock(), true);
  const err = new Error("terminating connection due to administrator command");
  client.emit("error", err);
  assert.deepEqual(lost, [err]);
});

test("a refused lock is not reported as lost, and its connection goes back clean", async () => {
  const { db, client, lost } = watched();
  client.next = { rows: [{ ok: false }] };
  assert.equal(await db.acquireInstanceLock(), false);
  assert.equal(client.listenerCount("error"), 0, "no stale listener left on a pooled client");
  assert.deepEqual(lost, []);
});

test("letting go of the lock on purpose is not reported as lost", async () => {
  const { db, client, lost } = watched();
  await db.acquireInstanceLock();
  await db.close();
  client.emit("error", new Error("connection reset during shutdown")); // must not throw either
  assert.deepEqual(lost, []);
});

test("an idle pooled connection dying is survivable and does not throw", () => {
  const { pool, trouble } = watched();
  const err = new Error("terminating connection due to administrator command");
  // With no 'error' listener on the pool this emit would throw.
  assert.doesNotThrow(() => pool.emit("error", err));
  assert.deepEqual(trouble, [[err, "idle"]]);
});

test("a dead LISTEN connection is destroyed, reported as survivable, and not released twice", async () => {
  const { db, client, lost, trouble } = watched();
  client.next = { rows: [] };
  await db.listenForCommands(() => undefined);
  const err = new Error("connection terminated");
  client.emit("error", err);
  assert.deepEqual(client.released, [err], "released with the error, so the pool destroys it");
  assert.deepEqual(trouble, [[err, "listen"]]);
  assert.deepEqual(lost, [], "losing LISTEN must not look like losing the lock");
  await db.close();
  assert.equal(client.released.length, 1, "close() must not release the dead client again");
});

test("a LISTEN that fails does not leave its connection checked out", async () => {
  const { db, client } = watched();
  const err = new Error("permission denied");
  client.next = err;
  await assert.rejects(db.listenForCommands(() => undefined), err);
  assert.deepEqual(client.released, [err]);
});
