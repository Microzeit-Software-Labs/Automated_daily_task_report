# Interlock

Interlock turns the task list you already keep in a Google Sheet into a daily
report. At 09:00 and 17:00 it builds the report, **you approve it**, and it sends
it to your WhatsApp work groups as a table image with a one-line caption.
Nothing is ever sent without your approval.

- Reads your Google Sheet (read-only: it never changes the sheet).
- Opens a report at each report time and asks you in the browser.
- Sends it to the groups you choose, now or at a time you pick, and shows how each
  group went (with Retry).
- Runs in the background and starts when you log in to Windows.

**Contents:** [Install](#install) | [Using it](#using-it) | [How it works](#how-it-works) |
[The pieces](#the-pieces) | [Project structure](#project-structure) |
[Rules it keeps](#rules-it-keeps) | [Configuration](#configuration) |
[Limits](#limits-and-what-is-not-built) | [Developing](#developing)

## Install

On Windows 10 or 11, in this folder:

```
install.cmd
```

(Double-click it, or run it from a terminal.) It is safe to run again: it only does
what is missing. In order, it:

1. checks for Python 3.12+, Node.js 22+ and PostgreSQL 16, and offers to install any
   that are missing with `winget` (it asks first);
2. sets up the Python environment and builds the WhatsApp agent and the web app;
3. sets up the database (it asks for the PostgreSQL administrator password once);
4. asks for your Google Sheet link and checks it;
5. explains the WhatsApp risk (see below), and if you accept, shows a QR code to scan
   from your phone (WhatsApp > Linked Devices > Link a Device);
6. makes Interlock start in the background when you log in, starts it, and opens it.

Useful options: `install.cmd -CheckOnly` (report what is installed and what would be
done, and change nothing), `-Reconfigure` (ask about the sheet and WhatsApp again),
`-Yes` (accept the "install this program?" questions), `-SkipSheet`, `-SkipWhatsApp`.

### The WhatsApp risk

Interlock reaches your existing WhatsApp groups by acting as a linked device, using
an unofficial library (Baileys). **This is against WhatsApp's Terms of Service, and
WhatsApp can ban the number you link.** Interlock sends only reports you have
approved, a few messages a day, which keeps the footprint small but does not remove
the risk. The installer makes you type YES before it turns this on. Read
[docs/whatsapp-agent-setup.md](docs/whatsapp-agent-setup.md) first.

## Using it

Open http://127.0.0.1:8000/ (the installer opens it for you).

- **Reports** shows what needs your approval, and how each earlier report went.
  A popup also appears when a report opens at 09:00 and 17:00.
- **Groups** is where you pick the WhatsApp groups reports go to.
- **Settings** connects or changes the Google Sheet and the WhatsApp phone, and shows
  the schedule.

Day to day:

```powershell
.\scripts\start-interlock.ps1            # start (or open) it
.\scripts\start-interlock.ps1 -Status    # what is running, is WhatsApp connected
.\scripts\start-interlock.ps1 -Restart   # after changing any code
.\scripts\start-interlock.ps1 -Stop
.\scripts\register-autostart.ps1 -Remove # stop it starting at login
```

Logs are in `logs\` (`api`, `worker`, `agent` and `supervisor`). The settings you can
edit by hand are in `.env`; see [Configuration](#configuration).

## How it works

One day, end to end:

1. **Your sheet becomes the task list.** About once a minute the worker downloads the
   sheet's public CSV export and mirrors it into Interlock's own `tasks` table. Rows
   are matched by their `Sr No.`, and each task remembers which sheet it came from, so
   switching to a different sheet hides the old sheet's tasks instead of overwriting
   them (reading the old sheet again brings the same tasks back). Rows marked as
   delegated to someone else are left out of reports. Rows with a blank or duplicate
   `Sr No.` are skipped and named in the worker log. Nothing is ever written to the
   sheet.
2. **A report opens at 09:00 and 17:00** on working days. The worker creates one review
   per time per day (a missed time is still created when the laptop wakes up) and the
   web page pops up. You can snooze it ("remind me again in 30 minutes").
3. **You review and approve.** The report is a table of the current tasks with overdue
   stated in words. You pick the groups and either send now or choose a time. Approving
   freezes a **snapshot**: the exact table image and caption that will be sent, so
   edits made later never change what you approved.
4. **The worker sends it.** Approval creates a share job with one recipient per group.
   Every 30 seconds the worker sends the jobs that are due, through the agent. Each
   group succeeds or fails on its own ("Sent to 3 groups", "Partly sent 2/3"), a
   temporary failure is retried after 0 s, 30 s, 2 min and 10 min, and a failed group
   has a Retry button.
5. **If the moment was missed** (the laptop was asleep, WhatsApp was disconnected) the
   report is judged by how stale it is, not how late: a few minutes late still sends, a
   report frozen yesterday never goes out today. See [Rules it keeps](#rules-it-keeps).

How a report moves, in the words used by the code:

| Stage | States |
|---|---|
| The review (what you decide) | `REVIEW_PENDING` -> `USER_EDITING` -> `READY` -> `APPROVED`, or `CLOSED_NO_SHARE` / `CANCELLED` / `EXPIRED` |
| The share job (one approval) | `SCHEDULED` / `RESCHEDULED` / `DEFERRED` -> `SENDING` -> `SENT`, `PARTIALLY_SENT` or `FAILED` (or `CANCELLED` / `SUPERSEDED`) |
| Each group | `QUEUED` -> `SENDING` -> `SENT`, `FAILED`, `SKIPPED` or `CANCELLED` |

A job's state is never set directly: it is worked out from its groups, which is what
lets "2 sent, 1 failed" be represented honestly.

## The pieces

Five things run on your computer. Nothing is hosted anywhere else.

| Piece | What it is | What it does |
|---|---|---|
| **API** | Python, FastAPI, on `127.0.0.1:8000` (this computer only) | Serves the web UI and the JSON API the UI uses. |
| **Worker** | Python, one process | The clock. Every 30 s it opens the 09:00/17:00 reviews and sends due jobs; every minute it reads the sheet. |
| **Agent** | Node.js + TypeScript, Baileys | The only thing that holds the WhatsApp session. Sends messages, looks up groups, links a phone. |
| **Supervisor** | `src/interlock/supervisor.py` | Starts the three above hidden, restarts any that crash after a short delay, makes sure only one agent runs, and writes `logs\`. Started at login by a Scheduled Task. |
| **PostgreSQL 16** | A Windows service | The only shared state, and the schedule of record: what to send and when lives in the database, so a restart loses nothing. |

There is deliberately no Docker, Redis or Celery: Postgres alone coordinates the
worker (jobs are claimed with `FOR UPDATE SKIP LOCKED`).

**Python and the agent never talk to each other directly.** They meet in two
PostgreSQL tables: Python writes a command (send this, find this group) and waits for
its result; the agent claims it, does it, and writes the result. The agent connects as
its own database role that can touch only those two tables.

## Project structure

```
interlock/
  install.cmd, install.ps1       one-command installer (re-runnable; -CheckOnly changes nothing)
  README.md                      this file
  HANDOVER.md                    for developers: decisions and why, bugs already fixed, history
  pyproject.toml, alembic.ini    the Python project; database migration settings
  .env, .env.example             your settings (.env is private and never committed)

  src/interlock/                 the Python backend
    domain/                      pure business rules: no database, web or network code
      approvals/                 the review state machine; the 09:00/17:00 popup rules
      sharing/                   snapshot, table layout, the missed-window rule, jobs, groups
      sync/                      parsing the sheet; remembering which sheet tasks came from
      tasks/  audit/  common/    task rules; the tamper-evident audit chain; clock, ids, errors
      scheduling/  ports/        scheduled actions; the interfaces the outside world must meet
    adapters/                    everything that touches the outside world
      persistence/               PostgreSQL via SQLAlchemy: models, repositories, audit log
      sheets/                    csv_source (the read-only reader in use); google_sheets (optional two-way)
      whatsapp/                  local_agent (real, via the agent); mock (tests)
      rendering/                 table_image.py: draws the report as a PNG
    services/                    the use cases: review, trigger, sharing, scheduler, sheet import
    api/                         FastAPI app, routers (approvals, shares, sheet, whatsapp, ...)
    workers/loop.py              the worker
    supervisor.py                the process supervisor
    config.py                    settings, read from .env
    setup_cli.py, envfile.py     the installer's interactive steps; safe .env editing

  apps/
    agent/                       the WhatsApp agent (Node + TypeScript)
      src/                       baileys.ts (the socket), commands.ts, db.ts, linkController.ts,
                                 rateLimiter.ts, circuitBreaker.ts, canary.ts, index.ts
      sql/agent_queries.sql      the exact statements the agent runs (shared with the tests)
    web/                         the web UI (React + Vite), served by the API
      src/pages/                 Dashboard (Reports), Review (one report), Groups, Settings
      src/components/            dialogs and banners: WhatsApp link, sheet link, report preview
      src/logic.ts               the wording and rules the UI applies, as pure tested functions

  migrations/versions/           database migrations (Alembic)
  scripts/                       start-interlock, register-autostart, setup-database,
                                 setup-agent-role, reset-postgres-password, export_openapi, ...
  docs/                          setup guides and docs/openapi.json (the API contract)
  tests/                         unit/, integration/ (real PostgreSQL), acceptance/ (whole flows)
```

The backend is layered, and the layering is enforced: `domain/` imports nothing from
`adapters/` (nor SQLAlchemy, FastAPI or HTTP libraries), and an automated test fails
if that is broken.

## Rules it keeps

- **Nothing sends without a human.** The state machine lets only a person reach
  `APPROVED`; no code path can approve on its own.
- **What you approved is what is sent.** The snapshot is frozen at approval and the
  database refuses to change it afterwards.
- **A report never goes out on a different day than it was approved.** This is checked
  before the grace window (default 60 minutes) and overrides it. Jobs deferred for
  longer than 48 hours cancel themselves, and are audited.
- **Sending twice is prevented twice over:** an `Idempotency-Key` on the request, and a
  database uniqueness constraint behind it, proven under 50 concurrent requests.
- **Every change is audited, tamper-evidently.** Audit rows form a hash chain, and the
  application's database role cannot update or delete them.
- **Least privilege.** Three database roles: `interlock_owner` (schema and migrations),
  `interlock_app` (what the app runs as; no schema changes, no editing the audit log
  or snapshots), `interlock_agent` (two tables only).
- **Exactly one WhatsApp agent runs**, enforced by the supervisor, a database lock in
  the agent itself, and a sweep at start-up. If the agent loses its database
  connection it exits and is restarted, rather than carrying on without the lock.
- **All times are stored in UTC** and shown in your time zone; every database
  connection is pinned to UTC so timestamps round-trip exactly.

## Configuration

Settings live in `.env` (copy of `.env.example`, which explains every one). The ones
you are most likely to change:

| Setting | Default | Meaning |
|---|---|---|
| `TIMEZONE` | `Asia/Kolkata` | Used for "today", due dates and the report times |
| `MORNING_ALERT_TIME`, `EVENING_ALERT_TIME` | `09:00`, `17:00` | When reports open |
| `WORKING_DAYS` | `0,1,2,3,4` | 0 = Monday ... 6 = Sunday |
| `SEND_GRACE_MINUTES` | `60` | How stale an approved report may be and still send unattended |
| `RETRY_DELAYS_SECONDS` | `0,30,120,600` | Wait before each attempt; its length is the maximum attempts |
| `SHEET_SYNC_INTERVAL_SECONDS` | `60` | How often the sheet is read |
| `REPORT_FORMAT` | `image` | `image` (table picture + caption) or `text` |
| `DEFAULT_USER_NAME` | `Kaif` | The name shown as the person approving |

**The installer does not ask for the time zone, report times, working days or your
name**, so a new install starts with the defaults above. Edit `.env` and run
`.\scripts\start-interlock.ps1 -Restart`. The Google Sheet link and the WhatsApp phone
are not in `.env`: they are changed in **Settings** and saved in the database.

## Limits and what is not built

- **Windows only**, and **PostgreSQL 16 only** (the setup scripts expect it in
  `C:\Program Files\PostgreSQL\16`).
- **One person, no login.** Every request acts as the one configured user, and the API
  only listens on this computer. It is not a multi-user system.
- **The sheet is read, never written.** Task edits happen in the sheet. A two-way sync
  with a Google service account exists in the code but is switched off and has never
  been run against a real sheet.
- **WhatsApp can ban the linked number** (see above). The agent rate-limits itself and
  stops sending when it sees repeated failures, which lowers the risk, not to zero.
- Not built: a holiday calendar, editable message templates, push notifications, an
  uninstaller (by hand: `register-autostart.ps1 -Remove`, then
  `start-interlock.ps1 -Stop`), and the official WhatsApp Cloud API.
- The installer has been run on the machine it was written on, not yet on a fresh one.

## Developing

```powershell
.\.venv\Scripts\python.exe -m pytest tests          # backend (needs the test database)
.\.venv\Scripts\python.exe -m mypy src/interlock
.\.venv\Scripts\python.exe -m ruff check src tests migrations scripts
cd apps\web;   npm test      # type-check + unit tests; npm run build to rebuild the UI
cd apps\agent; npm test
```

- The backend tests run against **real PostgreSQL** (`interlock_test`, separate from
  the live `interlock` database), with a frozen clock and a mock WhatsApp, so they are
  safe to run while Interlock is running. Acceptance tests drive the whole flow through
  the real API. There is a coverage gate of 90% on `domain/` (run it over all of
  `tests/`, not just `tests/unit/`).
- After any backend change, `.\scripts\start-interlock.ps1 -Restart`: the rebuilt UI is
  shared and can be newer than the running API.
- After a change to a route or schema, regenerate the contract:
  `.\.venv\Scripts\python.exe scripts/export_openapi.py` (writes `docs/openapi.json`).
- Database changes go in a new Alembic migration, applied with
  `.\.venv\Scripts\python.exe -m alembic upgrade head`.
- [HANDOVER.md](HANDOVER.md) is the place to start before changing behaviour: it records
  what each part is for, why it was built that way, and the bugs already found.

More: [docs/web-ui.md](docs/web-ui.md), [docs/sheet-import-setup.md](docs/sheet-import-setup.md),
[docs/whatsapp-agent-setup.md](docs/whatsapp-agent-setup.md),
[docs/google-sheets-setup.md](docs/google-sheets-setup.md).
