/** The agent's side of the whatsapp_agent_commands / whatsapp_agent_status
 * outbox -- see src/interlock/adapters/persistence/models/whatsapp_agent.py
 * for the tables and apps/agent/sql/agent_queries.sql for the statements
 * this class runs. Python only ever inserts a command and reads a result
 * (adapters/whatsapp/local_agent.py); this class is the only thing that
 * ever claims, completes, releases, or resets a row.
 */
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Pool, PoolClient } from "pg";
import type { PairingProgress, PairingStore } from "./linkController";
import { loadNamedQueries, toPositionalQuery } from "./sqlNamedQueries";

// Compiled output runs from dist/src/, so the project root is two levels up.
const SQL_PATH = join(__dirname, "..", "..", "sql", "agent_queries.sql");
const NOTIFY_CHANNEL = "whatsapp_agent_commands";
// Two-int advisory lock key: "only one agent per database". Held for the
// life of the process by a dedicated connection.
const INSTANCE_LOCK = [738201, 1] as const;

export interface ClaimedCommand {
  id: string;
  op: "resolve_group" | "send_text" | "test_send" | "link_start" | "link_cancel" | "reconnect";
  payload: Record<string, unknown>;
  client_message_id: string | null;
  wa_message_id: string | null;
  created_at: string;
  expires_at: string;
}

export interface AgentStatusUpdate {
  state: string;
  detail: string;
  /** Machine-readable why, for the UI's banner; null while healthy. */
  reason: string | null;
  accountJid: string | null;
  accountName: string | null;
  agentVersion: string | null;
  lastSuccessfulSendAt: Date | null;
  lastCanaryAt: Date | null;
  lastCanaryOk: boolean | null;
}

export class AgentDb implements PairingStore {
  private readonly pool: Pool;
  private readonly queries: Record<string, string>;
  private listenClient: PoolClient | null = null;
  private lockClient: PoolClient | null = null;

  constructor(connectionString: string) {
    this.pool = new Pool({ connectionString });
    this.queries = loadNamedQueries(readFileSync(SQL_PATH, "utf-8"));
  }

  private query(name: string): string {
    const sql = this.queries[name];
    if (!sql) {
      throw new Error(`agent_queries.sql has no statement named "${name}"`);
    }
    return sql;
  }

  private async run<Row extends object = Record<string, unknown>>(
    name: string,
    params: Record<string, unknown>
  ): Promise<Row[]> {
    const { text, values } = toPositionalQuery(this.query(name), params);
    const result = await this.pool.query<Row>(text, values);
    return result.rows;
  }

  /** Take the one-agent-per-database lock. False means another agent holds it.
   *
   * Must happen before anything that assumes it is the only agent (notably
   * reset_stale_claims, which would otherwise steal a live peer's work). */
  async acquireInstanceLock(): Promise<boolean> {
    const client = await this.pool.connect();
    const { rows } = await client.query<{ ok: boolean }>(
      "SELECT pg_try_advisory_lock($1, $2) AS ok",
      [...INSTANCE_LOCK]
    );
    if (!rows[0]?.ok) {
      client.release();
      return false;
    }
    this.lockClient = client;
    return true;
  }

  /** Like claimNext, but only link-management commands (see the SQL). */
  async claimNextControl(claimedBy: string, marginSeconds: number): Promise<ClaimedCommand | null> {
    const rows = await this.run<ClaimedCommand>("claim_next_control", {
      claimed_by: claimedBy,
      margin_seconds: marginSeconds,
    });
    return rows[0] ?? null;
  }

  async updatePairing(progress: PairingProgress): Promise<void> {
    await this.run("update_pairing", {
      pairing_state: progress.state,
      pairing_id: progress.pairingId,
      pairing_qr: progress.qr,
      pairing_qr_at: progress.qrAt,
      pairing_detail: progress.detail,
    });
  }

  async claimNext(claimedBy: string, marginSeconds: number): Promise<ClaimedCommand | null> {
    const rows = await this.run<ClaimedCommand>("claim_next", {
      claimed_by: claimedBy,
      margin_seconds: marginSeconds,
    });
    return rows[0] ?? null;
  }

  async complete(commandId: string, result: unknown): Promise<void> {
    await this.run("complete", { id: commandId, result: JSON.stringify(result) });
  }

  async releaseTransient(commandId: string): Promise<void> {
    await this.run("release_transient", { id: commandId });
  }

  async resetStaleClaims(): Promise<void> {
    await this.run("reset_stale_claims", {});
  }

  async setWaMessageId(commandId: string, waMessageId: string): Promise<void> {
    await this.run("set_wa_message_id", { id: commandId, wa_message_id: waMessageId });
  }

  async upsertStatus(status: AgentStatusUpdate): Promise<void> {
    await this.run("upsert_status", {
      state: status.state,
      detail: status.detail,
      status_reason: status.reason,
      account_jid: status.accountJid,
      account_name: status.accountName,
      agent_version: status.agentVersion,
      last_successful_send_at: status.lastSuccessfulSendAt,
      last_canary_at: status.lastCanaryAt,
      last_canary_ok: status.lastCanaryOk,
    });
  }

  /** The one write this class makes outside the shared contract file: the
   * weekly canary (canary.ts) enqueues itself, which is not something
   * Python ever needs to do and so is not part of agent_queries.sql. */
  async enqueueTestSend(reason: string, timeoutSeconds: number): Promise<string> {
    const id = randomUUID();
    const now = new Date();
    const expiresAt = new Date(now.getTime() + timeoutSeconds * 1000);
    await this.pool.query(
      `INSERT INTO whatsapp_agent_commands (id, op, payload, status, created_at, expires_at)
       VALUES ($1, 'test_send', $2, 'PENDING', $3, $4)`,
      [id, JSON.stringify({ reason }), now, expiresAt]
    );
    return id;
  }

  /** LISTEN on a dedicated connection so a notification wakes the claim
   * loop promptly; the loop's own fixed poll interval (see index.ts) is the
   * fallback if this connection ever drops -- belt-and-suspenders, not the
   * only path to progress. */
  async listenForCommands(onNotify: () => void): Promise<PoolClient> {
    const client = await this.pool.connect();
    await client.query(`LISTEN ${NOTIFY_CHANNEL}`);
    client.on("notification", () => onNotify());
    this.listenClient = client;
    return client;
  }

  /** pool.end() waits for every checked-out client, so the LISTEN client
   * must be released first or shutdown hangs forever. */
  async close(): Promise<void> {
    this.listenClient?.release();
    this.listenClient = null;
    if (this.lockClient) {
      // Unlock explicitly (release() alone returns the connection to the pool
      // with the session-level lock still held), then let go of it.
      await this.lockClient
        .query("SELECT pg_advisory_unlock($1, $2)", [...INSTANCE_LOCK])
        .catch(() => undefined);
      this.lockClient.release();
      this.lockClient = null;
    }
    await this.pool.end();
  }
}
