# Real WhatsApp delivery — setup

Interlock's `local_agent` provider sends approved reports to real WhatsApp groups
through a linked device, using [Baileys](https://github.com/WhiskeySockets/Baileys) — an
unofficial client library. Read this whole section before turning it on.

## The risk, up front

Meta's official WhatsApp API cannot post to a pre-existing consumer group at all — it
only serves groups the business itself creates through the API, capped at 8 members
(see `HANDOVER.md`'s WhatsApp section and the Phase 0 architecture doc's §8). Baileys is
the only thing that reaches your actual groups, and it does so by impersonating a normal
WhatsApp client. **This violates WhatsApp's Terms of Service.** Field reports describe
session logouts and bans, sometimes within hours, mostly for bot-like usage (24/7,
datacenter IPs, auto-replying). Interlock's profile is different — a laptop's residential
IP, outbound-only, human-approved sends a few times a day — but the risk is real, not
theoretical.

**Strongly use a dedicated number**, not your personal WhatsApp — a second SIM or eSIM,
so a ban costs a spare number rather than your real account. `WHATSAPP_PROVIDER=local_agent`
only takes effect once you also set `WHATSAPP_LOCAL_AGENT_TOS_ACK=true` — a second,
deliberate flag, precisely so this can't be switched on by accident.

Out of the box `WHATSAPP_PROVIDER=mock` records sends in memory and touches nothing real;
every acceptance test runs against it. Nothing below is required until you actually want
live delivery.

## 1. Install Node dependencies

```powershell
cd apps\agent
npm install
```

Node 22 is required (already installed on this machine — see `HANDOVER.md`). Dependency
versions are exact-pinned in `package.json` and `package-lock.json` is committed:
upgrading (Baileys especially, since it tracks a protocol Meta can change without notice)
is a deliberate, tested action, never a routine `npm update`.

## 2. Provision the agent's database role

Prerequisites: `scripts/setup-database.ps1` has already run, and migrations are at head
(`.\.venv\Scripts\python.exe -m alembic upgrade head`).

```powershell
.\scripts\setup-agent-role.ps1
```

Creates (or rotates) a Postgres role, `interlock_agent`, granted **only**
`SELECT, INSERT, UPDATE` on two tables: `whatsapp_agent_commands` and
`whatsapp_agent_status`. It cannot read `tasks`, `report_snapshots`, `approval_requests`,
or `audit_logs`. This role is the whole trust boundary between the agent process and
everything else — see "How it works" below for why there's no signed-command protocol on
top of it. Writes `apps\agent\.env` (gitignored); never touches the project root `.env`.

## 3. Pair the device (one time)

```powershell
cd apps\agent
npm start
```

A QR code prints in the terminal. On the phone that will run the agent: **WhatsApp →
Linked Devices → Link a Device**, and scan it. Once linked, session state is saved to
`apps\agent\.wa-session\` (gitignored) and pairing persists across restarts — you do this
once, not every time you start the agent.

Confirm the log line `agent.connected`, then stop it (Ctrl+C) — steady-state running is
step 6.

## 4. Turn it on in Interlock

In the project root `.env`:

```dotenv
WHATSAPP_PROVIDER=local_agent
WHATSAPP_LOCAL_AGENT_TOS_ACK=true
```

Restart the API and the worker. If the ack line is missing, both refuse to start with a
message pointing back here — that's deliberate, not a bug.

## 5. Pin your real groups

There's no frontend yet, so use the API directly (`http://127.0.0.1:8000/docs` or `curl`):

```
POST /whatsapp/groups/resolve   {"name": "SI Team"}
```

Returns every group whose name matches, ranked by closeness — never just one. **The
linked device must already be a member of the group** (and, if the group restricts who
can post, allowed to send). Pick the right `external_jid` from the response, then:

```
POST /whatsapp/groups   {"external_jid": "...", "display_name": "SI Team", ...}
```

The JID is pinned permanently once added — a later group rename never silently redirects
where reports go.

## 6. Run it day to day: a Scheduled Task

Don't leave a terminal window open. Register it to start at login and restart itself if
it crashes:

```powershell
$action = New-ScheduledTaskAction -Execute "node.exe" `
    -Argument "dist\src\index.js" -WorkingDirectory "C:\Users\Admin\Documents\interlock\apps\agent"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Interlock WhatsApp Agent" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Baileys WhatsApp linked-device agent for Interlock"
```

Build first (`npm run build`, inside `apps\agent`) so `dist\src\index.js` exists — the task
runs the compiled output, not `ts-node`. **`-MultipleInstances IgnoreNew` is not
optional**: two agent processes sharing one `.wa-session` folder will corrupt it or get
the linked device kicked by WhatsApp. Logs go to `apps\agent\logs\agent.log`, not the
console — there's no terminal to read once this runs headless under Task Scheduler.

## How it works

Two Postgres tables are the entire interface between Python and the agent (see
`src/interlock/adapters/persistence/models/whatsapp_agent.py`):

- `whatsapp_agent_commands` — an outbox. Python inserts a `send_text` / `resolve_group`
  row and polls for it to reach `DONE`; the agent claims the oldest pending row
  (`FOR UPDATE SKIP LOCKED`, the same pattern the scheduler already uses), sends, and
  writes the result back.
- `whatsapp_agent_status` — a single row the agent overwrites on a heartbeat, so
  `GET /whatsapp/status` and `/readiness` are one cheap read, never a live round trip to
  the agent.

The original Phase 0 design specified a signed (Ed25519), outbound-only WebSocket
protocol for this. It was built for a Dockerised backend with one process to dial into;
what actually exists is two plain Python processes (the API and the worker) and no
asyncio anywhere in the codebase. The Postgres outbox above is the deliberate replacement
— the `interlock_agent` role, scoped to exactly these two tables, is the trust boundary
instead of a signed envelope. Full reasoning in `HANDOVER.md`'s WhatsApp section.

A send's WhatsApp message id is generated and persisted *before* the agent calls
`sendMessage` — so if the agent crashes mid-send, the row (reset from `CLAIMED` back to
`PENDING` on the agent's next startup) is retried with the **same** id, and WhatsApp
naturally deduplicates. Every command also carries an expiry: if Interlock gives up
waiting before the agent ever claims a row, the row expires and is never claimed late —
never sent silently after the recipient was already marked `FAILED`.

## Monitoring health

```
GET /whatsapp/status
GET /readiness
```

| `state` | Meaning |
|---|---|
| `CONNECTED` | Normal. |
| `CONNECTING` | Reconnecting after a drop — transient, don't panic. |
| `LOGIN_REQUIRED` | Logged out. Re-run step 3. |
| `UNAVAILABLE` | Agent hasn't reported a heartbeat recently (default: 90s) — check it's running. |
| `AUTOMATION_ERROR` | The circuit breaker tripped (5 consecutive send failures) or the weekly canary failed while the connection looked healthy. Investigate before restarting. |

The weekly canary sends a message to the linked account's own number ("Message
Yourself") through the same outbox as everything else, so its history is queryable
alongside real sends. To check sooner than a week:

```powershell
.\.venv\Scripts\python.exe scripts\whatsapp_test_send.py
```

## Things to know

- **Recovering from a logout**: the agent does not auto-reconnect after
  `LOGIN_REQUIRED` — repeat step 3. `.wa-session\` is local device state, not a backup of
  anything; losing it means re-pairing, not data loss.
- **Sends are serialized through one process.** A job with several recipients dispatches
  one at a time, each waiting up to `WHATSAPP_AGENT_COMMAND_TIMEOUT_SECONDS` (default 20s)
  in the worst case. Fine at this tool's scale (a handful of groups); worth knowing if
  that ever grows.
- **No push alerts.** Health is pull-only (`/whatsapp/status`, `/readiness`, the log
  file) — nothing pages you when `AUTOMATION_ERROR` fires.
- **Rate limiting is in-process, not persisted.** A minimum 3s gap between sends and an
  hourly cap reset on every agent restart — restarting does not bypass WhatsApp's own
  server-side throttling, only this app's self-imposed one.
- **`whatsapp_agent_commands` has no retention job.** At a few sends a day this is a
  non-issue for a long time; a manual cleanup query is the documented fallback if it ever
  matters.
