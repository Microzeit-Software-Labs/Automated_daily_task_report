/** Dispatches one claimed command to the right handler. send_text and
 * test_send (the weekly canary, see canary.ts) share a path: the only
 * difference is where the message goes and what it says.
 */
import { randomUUID } from "node:crypto";
import { AgentDb, ClaimedCommand } from "./db";
import { BaileysAgent, GroupInfo } from "./baileys";
import { CircuitBreaker } from "./circuitBreaker";
import { RateLimiter } from "./rateLimiter";
import { logger } from "./logger";

export interface CommandContext {
  db: AgentDb;
  baileys: BaileysAgent;
  rateLimiter: RateLimiter;
  breaker: CircuitBreaker;
}

export interface RankedCandidate {
  external_jid: string;
  display_name: string;
  member_count: number | null;
  confidence: number;
}

/** Exactly MockWhatsAppProvider.resolve_group's rule (see
 * src/interlock/adapters/whatsapp/mock.py) so a developer testing against
 * the mock and production against this agent see the same ranking. */
export function rankGroups(name: string, groups: GroupInfo[]): RankedCandidate[] {
  const needle = name.trim().toLowerCase();
  if (!needle) {
    return [];
  }

  const matches: RankedCandidate[] = [];
  for (const group of groups) {
    const haystack = group.subject.toLowerCase();
    let confidence: number;
    if (haystack === needle) {
      confidence = 1.0;
    } else if (haystack.startsWith(needle)) {
      confidence = 0.8;
    } else if (haystack.includes(needle)) {
      confidence = 0.6;
    } else {
      continue;
    }
    matches.push({
      external_jid: group.jid,
      display_name: group.subject,
      member_count: group.memberCount,
      confidence,
    });
  }

  matches.sort(
    (a, b) => b.confidence - a.confidence || a.display_name.localeCompare(b.display_name)
  );
  return matches;
}

export async function processCommand(cmd: ClaimedCommand, ctx: CommandContext): Promise<void> {
  if (cmd.op === "resolve_group") {
    await processResolveGroup(cmd, ctx);
  } else if (cmd.op === "send_text" || cmd.op === "test_send") {
    await processSend(cmd, ctx);
  } else {
    // Unreachable given the DB's own CHECK constraint on op, but the
    // outbox is a shared contract -- a future op this build does not know
    // about must not crash the loop.
    await ctx.db.complete(cmd.id, {
      accepted: false,
      error_code: "UNKNOWN_OP",
      error_detail: String(cmd.op),
      error_class: "PERMANENT",
    });
  }
}

async function processResolveGroup(cmd: ClaimedCommand, ctx: CommandContext): Promise<void> {
  const name = String(cmd.payload["name"] ?? "");
  try {
    const groups = await ctx.baileys.fetchGroups();
    await ctx.db.complete(cmd.id, { candidates: rankGroups(name, groups) });
  } catch (err) {
    // Left CLAIMED, not completed as a failure: Python's own poll timeout
    // covers this (see local_agent.py), and the group set is just as
    // reachable on the very next attempt -- there is no reason to fabricate
    // a terminal PERMANENT verdict from what is very likely a transient
    // socket problem.
    logger.error({ err, commandId: cmd.id }, "resolve_group.failed");
  }
}

async function processSend(cmd: ClaimedCommand, ctx: CommandContext): Promise<void> {
  if (ctx.breaker.isOpen()) {
    logger.warn({ commandId: cmd.id }, "send.circuit_breaker_open");
    await ctx.db.releaseTransient(cmd.id);
    return;
  }
  if (!ctx.rateLimiter.canSendNow()) {
    logger.warn({ commandId: cmd.id }, "send.hourly_cap_reached");
    await ctx.db.releaseTransient(cmd.id);
    return;
  }

  const isCanary = cmd.op === "test_send";
  const jid = isCanary ? ctx.baileys.ownJid() : String(cmd.payload["external_jid"] ?? "");
  const body = isCanary
    ? `Interlock agent canary (${String(cmd.payload["reason"] ?? "test")}) -- ignore.`
    : String(cmd.payload["body"] ?? "");

  if (!jid) {
    // Not connected long enough to know our own jid yet, or a malformed
    // send_text payload -- either way, worth another attempt shortly
    // rather than a fabricated permanent failure.
    await ctx.db.releaseTransient(cmd.id);
    return;
  }

  await ctx.rateLimiter.waitForTurn();

  const waMessageId = cmd.wa_message_id ?? `interlock-${randomUUID()}`;
  if (!cmd.wa_message_id) {
    // Persisted before the actual send: a resend after a crash must reuse
    // this id, never mint a new one (see local_agent.py's module docstring).
    await ctx.db.setWaMessageId(cmd.id, waMessageId);
  }

  try {
    await ctx.baileys.send(jid, body, waMessageId, cmd.id);
    ctx.rateLimiter.recordSend();
    ctx.breaker.recordSuccess();
    // Completion is asynchronous from here -- BaileysAgent completes (or
    // releases) this row itself once a real delivery receipt arrives.
  } catch (err) {
    ctx.breaker.recordFailure();
    logger.error({ err, commandId: cmd.id, jid }, "send.threw");
    await ctx.db.releaseTransient(cmd.id);
  }
}
