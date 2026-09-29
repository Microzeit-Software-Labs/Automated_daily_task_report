-- The agent's side of the whatsapp_agent_commands / whatsapp_agent_status
-- contract (see src/interlock/adapters/persistence/models/whatsapp_agent.py
-- for the tables and the reasoning). This file is the single source of
-- truth for that contract: the Node agent runs it in production, and
-- tests/integration/test_local_agent_provider.py's FakeAgent runs the exact
-- same statements standing in for it, so a test proving Python's side of the
-- contract holds is proving it against production SQL, not a hand-copy that
-- can drift.
--
-- Placeholders are SQLAlchemy-style ":name" (bound directly via text() on
-- the Python/FakeAgent side). The Node agent's db.ts must translate ":name"
-- to pg's positional "$1, $2, ..." before calling pool.query() -- see the
-- loadNamedQueries() helper in db.ts.
--
-- Statements are separated by a "-- name: <name>" header line and run
-- through the next such header or end of file.

-- name: claim_next
-- One row at a time, oldest first, skipping anything already claimed by a
-- concurrent claimant (there should only ever be one agent process -- see
-- reset_stale_claims -- but SKIP LOCKED costs nothing and removes the
-- assumption from the query itself). Never claims a row with too little time
-- left to plausibly finish before Python's own poll gives up on it.
UPDATE whatsapp_agent_commands
SET status = 'CLAIMED', claimed_at = now(), claimed_by = :claimed_by
WHERE id = (
    SELECT id FROM whatsapp_agent_commands
    WHERE status = 'PENDING'
      AND expires_at > now() + (:margin_seconds || ' seconds')::interval
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id, op, payload, client_message_id, wa_message_id, created_at, expires_at;

-- name: complete
-- Terminal outcomes only: a send the WhatsApp server acknowledged, or a
-- failure that retrying cannot fix. :result is a JSON-encoded SendOutcome
-- fragment (accepted/ack/provider_message_id/error_code/error_detail/
-- error_class). Guarded by "status = 'CLAIMED'" so a row that was reset out
-- from under a slow agent (see reset_stale_claims) cannot be double-completed.
-- CAST(...), not "::jsonb" directly after the placeholder: SQLAlchemy's
-- text() bind-parameter parser does not reliably split ":result" from an
-- immediately adjacent "::" cast, so on the Python (FakeAgent) side that
-- would silently leave ":result::jsonb" unsubstituted. CAST(...) is
-- unambiguous SQL and works identically on the Node side.
UPDATE whatsapp_agent_commands
SET status = 'DONE', result = CAST(:result AS jsonb), completed_at = now()
WHERE id = :id AND status = 'CLAIMED'
RETURNING id;

-- name: release_transient
-- A retryable failure (offline, throttled, socket error mid-send with no
-- ack). Back to PENDING, not DONE -- MockWhatsAppProvider's own rule is that
-- a transient failure is never memoised, so a later attempt with the same
-- client_message_id genuinely re-attempts. wa_message_id is left untouched:
-- if a send did leave this machine before the failure, a resend must reuse
-- the same WhatsApp message key, not mint a new one.
UPDATE whatsapp_agent_commands
SET status = 'PENDING', claimed_at = NULL, claimed_by = NULL
WHERE id = :id AND status = 'CLAIMED';

-- name: reset_stale_claims
-- Run once, before the agent's main loop starts. Exactly one agent process
-- is ever supposed to run (see docs/whatsapp-agent-setup.md on
-- -MultipleInstances IgnoreNew), so any row still CLAIMED at startup is
-- definitionally left over from this same process's previous, crashed run --
-- never a peer's in-flight work to worry about disturbing.
UPDATE whatsapp_agent_commands
SET status = 'PENDING', claimed_at = NULL, claimed_by = NULL
WHERE status = 'CLAIMED';

-- name: set_wa_message_id
-- Written *before* the agent actually calls sendMessage, not after -- so if
-- the process dies between generating the id and getting an ack, the row
-- (once reset to PENDING by reset_stale_claims on restart) still carries the
-- id a resend must reuse.
UPDATE whatsapp_agent_commands
SET wa_message_id = :wa_message_id
WHERE id = :id AND status = 'CLAIMED';

-- name: upsert_status
-- The single-row heartbeat. Called on every connection.update and on a
-- fixed timer regardless of activity. NULL for last_successful_send_at /
-- last_canary_at / last_canary_ok means "no news on this front" -- the
-- COALESCE keeps whatever was last recorded rather than blanking it on a
-- heartbeat that has nothing new to report.
INSERT INTO whatsapp_agent_status
    (id, state, detail, agent_version, last_successful_send_at,
     last_canary_at, last_canary_ok, updated_at)
VALUES
    ('agent', :state, :detail, :agent_version, :last_successful_send_at,
     :last_canary_at, :last_canary_ok, now())
ON CONFLICT (id) DO UPDATE SET
    state = EXCLUDED.state,
    detail = EXCLUDED.detail,
    agent_version = EXCLUDED.agent_version,
    last_successful_send_at = COALESCE(
        EXCLUDED.last_successful_send_at, whatsapp_agent_status.last_successful_send_at
    ),
    last_canary_at = COALESCE(EXCLUDED.last_canary_at, whatsapp_agent_status.last_canary_at),
    last_canary_ok = COALESCE(EXCLUDED.last_canary_ok, whatsapp_agent_status.last_canary_ok),
    updated_at = now();
