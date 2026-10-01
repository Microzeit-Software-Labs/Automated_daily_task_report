# Interlock — Handover

**Project root:** `C:\Users\Admin\Documents\interlock`
**Repo state:** git on `main`, remote `origin` = `github.com/kaifas-collab/Automatied_task`. **The user's rule: do not commit or push without their explicit approval.** On 1 Oct 2026 they approved committing and pushing **M1-M3** (`9d55b1f`, `9c7e841`: pushed), then approved committing **the background service and M4** (two local commits after `9c7e841`). **Those two are not pushed:** pushing needs its own go-ahead. `git stash list` is empty.
**Phase status:** Phases 1–3 done and **in real use**: sheet import, approval, image reports and group sends have all run against real WhatsApp. A refinement round is in progress (§14): **M1-M4 done, plus the background service** (the first half of M5, built ahead of M4 at the user's request). The user has seen and approved M4. **Next: changing the Google Sheet link from the UI** (item 7 below), then the one-command installer.

---

## 0. START HERE (for a new Claude session)

**What the user wants:** Interlock working for real, ASAP, without cutting quality. The user is Kaif. Their real tasks live in the Google Sheet **"Kaif Task List"**:
`https://docs.google.com/spreadsheets/d/1dCIwGwZbLc3G2ziy9tWok0qfLKVN4CzQU_xE7k9SSig/edit#gid=0`
It's shared "anyone with the link can view". At 09:00 and 17:00 Interlock should build a report from that sheet, let the user approve it, and send it to their WhatsApp work groups.

**Verified working in real use (30 Sep 2026):** the import mirrors the user's real sheet; reports go out as a table image plus caption to real WhatsApp groups through the local agent; delivery shows per group. Setup is complete: `.env` holds `WHATSAPP_PROVIDER=local_agent`, the ToS ack and `SHEET_IMPORT_URL`, and the agent is paired (`apps/agent/.wa-session/`).

**The user's own decisions — don't re-ask:**
- **Task source = read-only import from their sheet** (§12). They declined a Google Cloud service account, and they edit tasks in the sheet, not in Interlock.
- Rows with status **"Subhan's Task" / "Safwan(a)'s Task" are left out of reports entirely** (they're delegated).
- **WhatsApp via Baileys**, knowing it breaks WhatsApp's ToS (§11). It's their **personal account**, which is in real work groups with their managers. The "Test" group (2 members) is for trying things.
- **Reports are a plain table image** (no colour coding), with overdue stated in words. **Frontend = Vite + React**, served by the API.
- **Interlock should run in the background and start at Windows login** (M5), and the installer should also work on other laptops, installing missing prerequisites via winget after asking.
- **No time estimates unless asked. Don't commit without approval.**

**Current work: the four-area refinement.** Plan: `C:\Users\Admin\.claude\plans\pasted-content-id-2df4-bismillah-proud-biscuit.md`. Status and details in §14.
1. **M1** scheduling correctness: done.
2. **M2** the 09:00/17:00 popup service: done.
3. **M3** WhatsApp session management (QR reconnect/change phone from the UI): done and verified on real WhatsApp (§14).
4. **Background service: done** (1 Oct, the user asked for it before the rest of M4): the supervisor, `start-interlock.ps1` as a start/stop/status controller, and a login task. See §14.
5. **M4** UI redesign (Reports page, report page, design system; Settings gets Sheet and Schedule cards): **done and committed (1 Oct); the user looked at it and is satisfied.** Verified in a real browser (§14).
6. **M5** the one-command installer (`install.cmd`, winget prerequisites, the database/sheet/QR steps, README): **last**, as the user asked. Its service half already exists (item 4).
7. **Next: add or change the Google Sheet link from the UI.** The user asked for it on 1 Oct and agreed it should work **like the WhatsApp linking**: a Settings card with the link and the last import ("Last read 11:52 · 96 tasks"), a **Change sheet / Connect a sheet** button, a step-by-step dialog (paste link → **Check**, showing the task count and first titles or a plain fix such as "Share → Anyone with the link → Viewer" → confirm → wait for the worker's first import → "96 tasks imported"), and a page-wide banner when the sheet can't be read. A failed check changes nothing; the new sheet is proven readable before it replaces the old. **Not started; get the user's go before building.** Three things the design must include (checked against the code on 1 Oct):
   - **Task identity must include the sheet.** The importer matches by `sr:<Sr No.>` only, across hidden tasks too (`sheet_import_service.py` `tick`), and un-hides any matched task whose row isn't delegated (`_update`). So "hide the old sheet's tasks" alone would be undone on the next tick and old tasks overwritten by the new sheet's rows. Scope the ref to `<spreadsheetId>:<gid>|sr:<n>` with a one-off migration stamping today's tasks with the current sheet; retire the old sheet's tasks with their own marker, not the delegation one (switching back then restores them).
   - **One source of truth: the database.** `.env`'s `SHEET_IMPORT_URL` only seeds it on first run; the M5 installer saves through the same check-and-save code. The worker reads the link every tick (today it is built once at startup in `workers/loop.py` `main`) and imports at once when it changes; `/config/ui` reads it from the database too. Keep refusing it alongside `SHEETS_PROVIDER=google` (`adapters/sheets/factory.py`).
   - **Record the last import** (time, task count, or the error) so the card and the banner can show it; today a failed import only reaches `logs\worker.log`. Audit each link change.

**Operational notes:**
- **Interlock now runs as a hidden background service** (§14): `.\scripts\start-interlock.ps1` starts it (and opens the browser), `-Status` shows what is running, `-Stop`, `-Restart`, `-Console` (foreground, live logs). `.\scripts\register-autostart.ps1` (already run on 1 Oct) starts it at login. Logs: `logs\{api,worker,agent,supervisor}.log`. **Exactly one agent may run**; the supervisor, the agent's own lock and a start-up sweep all enforce it. Don't start things by hand next to the service.
- After **any** backend change run `.\scripts\start-interlock.ps1 -Restart`: the rebuilt UI in `apps/web/dist` is shared and can be newer than the running API (e.g. it sends `delay_minutes`, which an older API rejects).
- **Running the test suite is safe while the service runs:** tests use `interlock_test`, the service uses `interlock`.

**How this was built — keep doing it:**
- Plan non-trivial work first (plan files live in `C:\Users\Admin\.claude\plans\`).
- Test against real Postgres, not mocks.
- Run the full battery in §7 before calling anything done.
- Every "decision" section below records *why*; read it before changing behaviour.
- The user likes short status updates. Answer "how long?" honestly if asked, but don't volunteer estimates. Don't commit without approval.

Read the rest of this file before touching code. It covers what exists, where, why it's built this way, and bugs already fixed so you don't rediscover them.

---

## 1. Where the requirements and design live

- **Phase 0 architecture** (the full design doc — state machines, ER diagram, WhatsApp feasibility analysis, risk register): published artifact at
  `https://claude.ai/code/artifact/618b0d36-31c8-4dad-b62b-9e0bcc8d886f`
  Read this first if anything below is unclear on *why* — it's the source of truth for every architectural decision.
- **Phase 1 implementation plan**: `C:\Users\Admin\.claude\plans\snoopy-giggling-graham.md`
  This is the 11-step build plan that was actually executed. Steps 1–11 are all done (see §3).
- **This file**: `C:\Users\Admin\Documents\interlock\HANDOVER.md`

---

## 2. Environment — what's already set up, what you need to check

| Thing | State |
|---|---|
| PostgreSQL 16.8 | Installed, running as a Windows service (`postgresql-x64-16`), native (not Docker) |
| Python | 3.12.10, venv at `C:\Users\Admin\Documents\interlock\.venv` |
| Node | 22.11.0 installed but **unused** — no frontend exists yet |
| `.env` | Exists, gitignored, holds real generated DB credentials. **Do not print its contents.** |
| Databases | `interlock` (dev) and `interlock_test` (test), both on `127.0.0.1:5432` |
| Roles | `interlock_owner` (DDL, runs migrations), `interlock_app` (no DDL — this is what makes the audit log genuinely append-only, see §5) |

**No Docker, no Redis, no Celery** — this is a deliberate architecture deviation from the original brief, agreed with the user. Postgres itself is the schedule of record; a single worker process ticks and claims work via `FOR UPDATE SKIP LOCKED`. See Phase 0 artifact "Deviation 1" and the Phase 1 plan's "Stack decisions" section for the full reasoning.

**To (re)provision the databases from scratch:**
```powershell
.\scripts\setup-database.ps1
```
Prompts for the Postgres superuser password interactively, creates both roles, both databases, and writes `.env`. Idempotent — safe to re-run (rotates the app/owner passwords each time).

**If you've lost the Postgres superuser password:** `.\scripts\reset-postgres-password.ps1` (must run from an **elevated** PowerShell). Read its docstring — it briefly enables passwordless local auth to reset the password, then restores the original config. Does not touch any data.

**Environment variable sourcing:** `.env` holds `DATABASE_URL` (app role → `interlock`), `MIGRATION_DATABASE_URL` (owner role → `interlock`), and `TEST_DATABASE_URL` (owner role → `interlock_test`). Every test and script loads `.env` via `python-dotenv`.

---

## 3. What's built — the 11-step plan, all done

| # | Step | Key files |
|---|---|---|
| 1 | Scaffold | `pyproject.toml`, `src/interlock/config.py`, `.env.example` |
| 2 | Domain core (pure, zero I/O) | `src/interlock/domain/` — see §4 |
| 3 | Persistence + migrations | `src/interlock/adapters/persistence/models/`, `migrations/versions/*.py` |
| 4 | Audit chain + unit of work | `src/interlock/domain/audit/chain.py`, `src/interlock/adapters/persistence/audit_sink.py`, `src/interlock/adapters/persistence/base.py` (`unit_of_work`) |
| 5 | Task service + CRUD API | `src/interlock/services/task_service.py`, `src/interlock/adapters/persistence/task_repository.py`, `src/interlock/api/routers/tasks.py` |
| 6 | Snapshot/render/mock provider | `src/interlock/domain/sharing/{snapshot,render}.py`, `src/interlock/adapters/whatsapp/mock.py` |
| 7 | Durable scheduler | `src/interlock/services/scheduler_service.py`, `src/interlock/adapters/persistence/scheduler_repository.py` |
| 8 | `/commit` endpoint | `src/interlock/services/sharing_service.py`, `src/interlock/api/routers/approvals.py`, `src/interlock/api/idempotency.py` |
| 9 | Worker loop / delivery | `src/interlock/workers/loop.py`, delivery logic lives inside `scheduler_service.py` |
| 10 | Acceptance tests (9 scenarios) | `tests/acceptance/scenarios/test_scenario_{1..9}_*.py` |
| 11 | OpenAPI export | `scripts/export_openapi.py` → `docs/openapi.json` |

Plus one thing **not** in the original 11 steps but built because it's load-bearing: **`src/interlock/services/review_trigger.py`** — the 09:00/17:00 review-creation logic. `scheduler_service.py` only handles *claiming and sending already-approved shares*; nothing else created reviews on schedule until this was added. The worker loop (`workers/loop.py::run_one_tick`) calls both `review_trigger.maybe_create_daily_reviews()` and `scheduler_service.tick()` every cycle, in separate transactions, so a failure in one never blocks the other.

### Full source tree
```
src/interlock/
├── config.py                          # pydantic-settings; NOTE: NoDecode gotcha, see §6
├── domain/                            # PURE — zero adapter/I-O imports, enforced by test
│   ├── common/
│   │   ├── actor.py                   # Actor, ActorKind (USER/SYSTEM/SYNC/AGENT)
│   │   ├── clock.py                   # Clock protocol, SystemClock, FrozenClock
│   │   ├── errors.py                  # DomainError hierarchy, stable `code` strings
│   │   └── ids.py                     # ULID + display-id formatting (TSK-00104 etc.)
│   ├── tasks/
│   │   ├── entities.py                # Task (frozen dataclass), apply_edit(), diff()
│   │   ├── derivations.py             # is_overdue/is_due_today/is_due_soon (PURE fns)
│   │   └── summary.py                 # TaskSummary, ChangeSummary
│   ├── approvals/
│   │   ├── states.py                  # ApprovalState/ShareJobState/RecipientState enums
│   │   ├── request.py                 # ApprovalRequest entity, ApprovalKind
│   │   └── machine.py                 # THE state machine — transition(), roll_up_job_state()
│   ├── sharing/
│   │   ├── snapshot.py                # freeze() — immutable report snapshot
│   │   ├── render.py                  # Jinja2 sandboxed template rendering
│   │   ├── validity.py                # THE missed-window rule — evaluate_send_validity()
│   │   ├── job.py                     # ShareJob, ShareRecipient entities
│   │   └── group.py                   # WhatsAppGroup entity
│   ├── sync/reconciliation.py         # Phase 2: decide_action() truth table, baseline, canonicalize
│   ├── scheduling/action.py           # ScheduledAction entity, ActionStatus
│   ├── audit/chain.py                 # Hash-chain algorithm, verify_chain()
│   └── ports/                         # Protocols: TaskRepository, WhatsAppProvider, AuditSink
├── adapters/
│   ├── sheets/                        # Phase 2 — see §10: columns.py, fake.py, google_sheets.py, factory.py
│   ├── persistence/
│   │   ├── models/                    # SQLAlchemy ORM — one file per bounded context
│   │   ├── base.py                    # make_engine() — UTC timezone pinning lives here!
│   │   ├── task_repository.py         # PostgresTaskRepository — atomic CAS update
│   │   ├── approval_repository.py     # idempotent create_scheduled()
│   │   ├── share_repository.py        # snapshot/job/recipient CRUD + CAS claim
│   │   ├── scheduler_repository.py    # SKIP LOCKED claim, lease sweep
│   │   ├── whatsapp_group_repository.py
│   │   ├── sequences.py               # PostgresSequences — display-id numbering
│   │   └── audit_sink.py              # PostgresAuditSink — advisory-lock serialized
│   └── whatsapp/mock.py               # MockWhatsAppProvider — the ONLY provider in Phase 1
├── services/                          # Orchestration layer (depends on ports, not adapters)
│   ├── task_service.py
│   ├── review_service.py              # review lifecycle (open/cancel/get_detail)
│   ├── review_trigger.py              # the 09:00/17:00 trigger — see above
│   ├── sharing_service.py             # THE /commit orchestration, 3 modes, CommitMode
│   └── scheduler_service.py           # THE worker tick — claim, validate, send, retry
├── api/                                # FastAPI — composition root is main.py
│   ├── main.py                        # create_app(settings=, clock=, whatsapp_provider=)
│   ├── deps.py                        # dependency providers, get_current_actor
│   ├── schemas.py                     # Pydantic — TaskEditFields uses REAL enum types!
│   ├── idempotency.py                 # HTTP Idempotency-Key middleware
│   ├── errors.py                      # DomainError → JSON envelope
│   └── routers/{tasks,approvals,whatsapp,shares,health}.py
└── workers/loop.py                     # `python -m interlock.workers.loop` entrypoint
```

---

## 4. Architecture rules that MUST be preserved

1. **`domain/` imports nothing from `adapters/`, and no SQLAlchemy/FastAPI/psycopg/httpx/alembic.** This is not just discipline — it's an automated test: `tests/unit/test_architecture_boundaries.py` parses every domain module's AST and fails if this is violated. If you add a domain module that needs a new capability, add a `Protocol` to `domain/ports/` and implement it in `adapters/`.

2. **State transitions only happen through `domain/approvals/machine.py::transition()`.** Never assign `.state = X` directly anywhere. `transition()` validates legality against a frozen table and enforces that only a human `Actor` can reach `ApprovalState.APPROVED` — this is the literal code-level enforcement of "nothing sends without human approval."

3. **A `ShareJob`'s state is never asserted, only rolled up** from its recipients via `roll_up_job_state()`. This is what makes "2 sent, 1 failed" representable.

4. **Report snapshots and audit log rows are immutable at the database level**, not just by convention: `interlock_app` has `UPDATE`/`DELETE` **revoked** on `report_snapshots` and `audit_logs` (see `migrations/versions/bdffd15a31c5_initial_schema.py`, the `REVOKE` statements near the end of `upgrade()`).

5. **Two-layer idempotency** for `/commit`: (a) HTTP-level `Idempotency-Key` header + `idempotency_keys` table (`api/idempotency.py`) prevents re-executing a request's side effects at all; (b) DB-level `UNIQUE(approval_request_id, action_version)` on `share_jobs` (`share_repository.py::create_job`) is the real backstop proven under actual concurrency — see `tests/integration/test_sharing_service.py::test_concurrent_commits_sharing_one_action_version_produce_exactly_one_job` (50 real threads, real separate transactions).

6. **Optimistic concurrency uses an atomic conditional UPDATE**, never fetch-then-compare-then-write. See `task_repository.py::update()`'s docstring — a fetch-then-write has a race window; `UPDATE ... WHERE id=:id AND version=:expected` does not.

---

## 5. The missed-window rule (the trickiest logic in the codebase)

Lives in `src/interlock/domain/sharing/validity.py::evaluate_send_validity()`. Distinguishes **lateness** (`now - run_at`, harmless — a laptop waking up 4 minutes late must still send) from **staleness** (`now - snapshot.created_at`, dangerous — a report frozen yesterday must never go out today).

**Check order matters and is tested**: day-boundary check runs *before* the grace-window check. A 23:55 approval with a 60-minute grace must defer at 00:30 (day boundary), not sail through because it's still "within grace." See `tests/unit/test_validity.py::test_day_boundary_beats_grace` — this test exists specifically because getting the order wrong is the natural mistake.

---

## 6. Bugs already found and fixed — don't rediscover these

These cost real debugging time. Documented so you don't repeat the process.

1. **Timezone bug (serious, silent):** `psycopg3` deserializes `TIMESTAMPTZ` in the **client session's local timezone** (this machine's OS locale, Asia/Calcutta), not UTC. A value written as `17:04 UTC` read back as `22:34+05:30` — same instant, different ISO string, which silently broke the audit hash chain (which hashes `occurred_at.isoformat()`). **Fixed** in `adapters/persistence/base.py::make_engine()` via a `connect` event that runs `SET TIME ZONE 'UTC'` on every connection. Also set at the database level (`ALTER DATABASE ... SET TimeZone TO 'UTC'`) as defense in depth. **If you ever create an engine anywhere without going through `make_engine()`, this bug comes back.**

2. **State machine gaps found via integration testing**, not designed in from the start:
   - `REVIEW_PENDING → APPROVED` and `USER_EDITING → APPROVED` had to be added as legal direct transitions (originally only `READY → APPROVED` was legal) because `/commit` represents one atomic action with no separate "mark ready" round-trip in Phase 1.
   - `PARTIALLY_SENT → SENT` is **not** a direct edge — a job must pass through `SENDING` first. `scheduler_service.py::retry_recipient()` explicitly transitions the job to `SENDING` before re-attempting, for exactly this reason.

3. **Manual retry vs. automatic retry use different idempotency keys, on purpose.** The mock provider (and any correct real provider) caches a **permanent** failure by `client_message_id` forever — so retrying with the *same* id can never succeed. `ShareRepository.reset_for_manual_retry()` mints a **fresh** `client_message_id` for a human-triggered retry (Scenario 8), while `requeue_recipient()` (the automatic transient-failure ladder) deliberately **reuses** the same id, because that's replaying the same logical attempt where the ambiguous-timeout case matters.

4. **`pydantic-settings` decodes "complex" types as JSON before your own validator runs.** `WORKING_DAYS=0,1,2,3,4` in `.env` is not valid JSON, so it broke `Settings()` construction entirely on first real use. Fixed with `Annotated[frozenset[int], NoDecode]` in `config.py` — see the comment there. **If you add a new `frozenset`/`tuple`/`list`-typed setting field, you need `NoDecode` + a `field_validator(mode="before")`, or it will silently work until someone actually loads `.env`.**

5. **Test-clock desync (cost real time to find):** acceptance tests were calling `trigger_reviews(now=MORNING)` and `run_tick(..., now=...)` with an explicit, hardcoded `now`, while the FastAPI app's own `clock` fixture (used internally for `/commit`'s implicit `send_at = now`) stayed frozen at an unrelated `T0`. The job's `run_at` and the test's tick time silently disagreed. **Fix:** every acceptance test that relies on implicit timing must call `clock.set_to(MORNING)` (or `EVENING`) before hitting the API — see any `test_scenario_*.py` file for the pattern. If you write a new acceptance test, do this first or you'll get a confusing "0 sent" failure.

6. **Starlette version surprise:** this environment has FastAPI 0.141.1 / Starlette 1.6.0 — much newer than typical training-data expectations. `app.include_router()` no longer eagerly flattens routes into `app.routes` (it wraps them in `_IncludedRouter` for lazy resolution). **Don't debug routing by inspecting `app.routes` directly** — use `TestClient` and hit `/openapi.json` or make real requests. Also: `app.add_middleware()` has a stricter structural type check against `BaseHTTPMiddleware` subclasses in this version that doesn't perfectly type-check even though it works correctly at runtime — see the `type: ignore[arg-type]` comment in `api/main.py`.

7. **A `method call in default argument` lint rule (ruff B008) flags FastAPI's own `Depends(...)` idiom** unless configured. Fixed via `[tool.ruff.lint.flake8-bugbear] extend-immutable-calls` in `pyproject.toml` — this is FastAPI's own documented recommendation, not a workaround.

8. **Variable name reuse across incompatible types within one function** tripped mypy three separate times this session (`sharing_service.py`, `scheduler_service.py`, `workers/loop.py`) — e.g. `outcome` typed as a `tuple[int,int]` from one call, then reassigned to a `bool | None` from a different call in the same function. mypy infers a variable's type from its *first* assignment in a function scope. Not a bug pattern to repeat — use distinct names per logical value.

---

## 7. How to verify everything still works

```powershell
cd C:\Users\Admin\Documents\interlock

# Full suite (488 tests: unit + integration + acceptance)
.\.venv\Scripts\python.exe -m pytest tests/ -v

# Domain coverage (gate: 90%, currently 96%). Must run over ALL of tests/, not
# tests/unit: several domain modules (dataclass-only entities) are exercised
# only by integration/acceptance tests, so unit-only reads ~80% and is not the gate.
.\.venv\Scripts\python.exe -m pytest tests --cov=interlock.domain --cov-report=term

# Type check (must be zero errors)
.\.venv\Scripts\python.exe -m mypy src/interlock

# Lint (must be zero errors)
.\.venv\Scripts\python.exe -m ruff check src tests migrations scripts

# Re-export the OpenAPI contract after any router/schema change
.\.venv\Scripts\python.exe scripts/export_openapi.py

# Run the worker loop for real (against interlock, not interlock_test)
.\.venv\Scripts\python.exe -m interlock.workers.loop
```

**The web UI (`apps/web/`) is verified separately too:** `cd apps\web; npm test` runs `tsc --noEmit` plus vitest (18 tests); `npm run build` must succeed.

**The Node agent (`apps/agent/`) is verified separately** — it's a different language and
toolchain, not part of the Python suite above:

```powershell
cd apps\agent
npm test    # tsc --strict, then node --test (29 tests: pure logic only, see §11)
```

**Acceptance tests specifically** (`tests/acceptance/`) drive the real FastAPI app via `TestClient` against `interlock_test`, with `FrozenClock` and `MockWhatsAppProvider` injected — no network, no real WhatsApp. They truncate the whole schema before each test (they can't use the integration suite's rollback-based isolation, since each HTTP call commits its own session, matching production). If you add a 10th scenario file, follow the pattern in any existing `test_scenario_*.py` — especially the `clock.set_to(...)` gotcha in §6.6.

---

## 8. What is explicitly NOT built (Phase 2+)

- **Excel task source.** (Google Sheets is built — §10.) Note it was built as a *peer* synced by `SheetSyncService`, **not** as an alternate `TaskRepository` implementation as this section originally planned; Postgres remains the only `TaskRepository`.
- **Official Cloud API WhatsApp provider.** (`local_agent`/Baileys is built — §11.) `WhatsAppProviderName.CLOUD_API` is reserved in `config.py` but `build_whatsapp_provider` raises `NotImplementedError` for it. Only useful for compliant 1:1 DMs or API-created groups (≤8 members) — see the Phase 0 doc's §8 before building it, the tradeoffs are the same ones that led to choosing Baileys first.
- **Real authentication.** Every request currently acts as one configured default user (`DEFAULT_USER_ID`/`DEFAULT_USER_NAME` in `.env`, resolved in `api/deps.py::get_current_actor`) with optional `X-Actor-Id`/`X-Actor-Name` header overrides. This is explicitly a placeholder — see the docstring in `config.py` on those settings fields.
- **Holiday calendar**, **template editing UI**, **notification delivery** (native toast / web push) — all deferred per the Phase 0 MVP scope cut, not forgotten.

---

## 9. If you're picking this up cold, in order

1. Read the Phase 0 architecture artifact (link in §1) — at least the executive summary and §8 (WhatsApp feasibility).
2. Read `C:\Users\Admin\.claude\plans\snoopy-giggling-graham.md` for the Phase 1 scope and exit criteria.
3. Run the full verification block in §7. Confirm 283 passed, mypy/ruff clean, before changing anything.
4. Read §6 (bugs already fixed) so you don't re-spend time on them.
5. Read §12 (the task source actually in use), §11 before touching the WhatsApp agent, and §10 only if the two-way sync ever comes back. Then follow §0's "Next work" order.

---

## 10. Google Sheets sync (Phase 2)

**Status:** built, wired into the worker, and tested against `FakeSpreadsheetProvider` and real Postgres. **Never run against a real Google Sheet** — `GoogleSheetsProvider` has no test at all (needs credentials). Setup steps: `docs/google-sheets-setup.md`.

**Model:** Postgres is the source of truth; the sheet is a *peer*. Each worker tick reads every sheet row and every task, and per task compares each side to the last point they agreed (`decide_action(db_changed, sheet_changed)` → NOOP / PUSH / PULL / CONFLICT). Both changed ⇒ nothing is touched and a row is parked in `sync_conflicts` for a human. A vanished row is a conflict, **never** a deletion (Phase 0 risk R5). Row identity is the `_sys` column (`task_id|version|hash`), never row position.

| Piece | Where |
|---|---|
| Pure rules (truth table, `resolve_baseline`, `canonicalize`, `ConflictResolution`) | `domain/sync/reconciliation.py` |
| Tick + `resolve_conflict` | `services/sheet_sync_service.py` |
| Cursor + conflict storage | `adapters/persistence/sync_repository.py`, `models/sync.py`, migration `ef1d9dacf6ad` |
| Providers | `adapters/sheets/{fake,google_sheets,factory,columns}.py` |
| HTTP | `GET /sync/conflicts`, `POST /sync/conflicts/{id}/resolve` (`api/routers/sync.py`) |
| Worker wiring | `workers/loop.py::run_one_tick` |

### Decisions worth knowing

- **The `_sys` stamp is a fallback baseline.** When a task has no cursor row but its sheet row carries a stamp (restored DB), the stamp is used instead of treating both sides as changed (`resolve_baseline`). The DB cursor still wins when present.
- **`canonicalize()` is applied to both sides before hashing.** `parse_field_value` strips whitespace on every read; a `Task` does not trim its title. Without it a title with trailing whitespace hashes differently after a real round trip and never converges. `FakeSpreadsheetProvider` stores dicts verbatim and never goes through `columns.py`, so **no fake-based test can catch this class of bug** — that is why `tests/unit/test_sheet_columns.py` tests the format/parse boundary directly.
- **`SHEETS_PROVIDER=mock` means the worker does not sync at all** (`run_one_tick(sheets=None)`). The fake forgets its rows on exit but the Postgres cursor does not, so ticking against it and later switching to `google` would park every task as "vanished".
- No `POST /sync/tick`: the worker owns ticking, same as the review trigger.

### Bugs found and fixed while adding coverage (§6 style — don't rediscover)

9. **Blank title cell crashed the whole tick.** `""` parses fine, so it slipped past the "drop unparseable cells" rule, then `Task.__post_init__` rejected it inside `apply_edit`. The exception is not caught per-row, so one typo rolled back *every* task's sync, every tick, forever. Now dropped in `_sheet_changes`.
10. **A mistyped date erased the due date.** `due_date` is nullable, and `_coerce` returns `None` for both "blank" and "unparseable", so `20/9/2026` cleared the field. Now only a genuinely blank nullable cell clears.
11. **Whitespace hash asymmetry** and 12. **mock-provider cursor poisoning** — see the decisions above.

### Known gaps — deliberately NOT fixed

- **Writes are positional; reads are by header name.** `_row_values` writes `_sys, display_id, title, … tags` left to right from column A, so a human reordering or inserting a column among A–K makes values land in the wrong cells (and the next read then parses garbage). The `columns.py` docstring used to claim order was free; it is now corrected, and the setup doc says to keep A–K fixed. A real fix means header-mapped, per-cell writes (and appends can't be per-cell). Extra columns to the right of K are safe.
- **Clearing `_sys` on an existing row creates a duplicate task** on the next tick (blank stamp = "human typed a new row").
- **A soft-deleted task's sheet row is orphaned** — nothing pushes or removes it. Nothing in the app sets `deleted_at` yet (no delete endpoint), so this is latent. Pinned by `TestSoftDeletedTask`.
- **A NOOP tick never re-creates a missing cursor row**; it only reappears on the next real change. Harmless, but the cursor table can lag the truth after a restore.
- **`task_history` is declared** (`models/tasks.py`, unique `(task_id, version)`) **but nothing writes it** — a Phase 1 gap, unrelated to sync. `_pull` audits via `audit_logs` only.
- Tags are not passed through `canonicalize` (a tag with surrounding whitespace would have the same asymmetry as a title). Unlikely in practice; not covered.

### Tests

`tests/unit/test_reconciliation.py`, `test_sheet_columns.py`; `tests/integration/test_sync_repository.py`, `test_sheet_sync_service.py`; `tests/acceptance/test_sync_conflicts.py` (HTTP + DI wiring), `test_worker_sheet_sync.py` (`run_one_tick`'s sync branch, which had no test at all). The acceptance `client` fixture now takes a `sheets` fixture (`FakeSpreadsheetProvider`).

---

## 11. Real WhatsApp delivery via a local Baileys agent (Phase 3)

**Status:** the `local_agent` provider is built, wired into both the API and the worker, and tested end-to-end against a Postgres-backed fake agent. **Never run against a real WhatsApp account** — `apps/agent/src/baileys.ts` (the actual Baileys socket) has no automated coverage at all, the same accepted gap `GoogleSheetsProvider` has for the real Sheets API (§10). Setup steps: `docs/whatsapp-agent-setup.md`. The user chose Baileys knowingly, over the compliant-but-can't-reach-existing-groups official Cloud API — see the Phase 0 doc's §8 / risk R1 and `docs/whatsapp-agent-setup.md`'s opening section before touching any of this.

**Model:** the agent (`apps/agent/`, Node.js + TypeScript, a separate process) is the only thing that holds the real WhatsApp session. Python and the agent share no code and no socket — they meet only through two Postgres tables, an outbox Python writes to and polls, that the agent claims from and completes. Trust boundary is a narrowly-scoped Postgres role (`interlock_agent`), not a signed protocol.

| Piece | Where |
|---|---|
| The port (unchanged) | `domain/ports/whatsapp.py` |
| Outbox tables | `models/whatsapp_agent.py`, migration `c7a41f9e2b30` |
| Python repository | `adapters/persistence/whatsapp_agent_repository.py` |
| `LocalAgentProvider` | `adapters/whatsapp/local_agent.py` |
| Provider gate | `adapters/whatsapp/factory.py` (`WHATSAPP_LOCAL_AGENT_TOS_ACK` required) |
| Shared SQL contract | `apps/agent/sql/agent_queries.sql` — run by the agent *and* by the Python test harness's FakeAgent, so it's one source of truth, not a hand-copy |
| The agent itself | `apps/agent/src/{index,db,baileys,commands,rateLimiter,circuitBreaker,canary}.ts` |
| Role + `.env` provisioning | `scripts/setup-agent-role.ps1` |
| Manual on-demand send | `scripts/whatsapp_test_send.py` |

### Why the transport is a Postgres outbox, not the Phase 0 doc's signed WebSocket

The Phase 0 architecture doc specified an Ed25519-signed, outbound-only WSS protocol for the agent, designed for a Dockerised backend with exactly one process to dial into. What actually got built (Deviation 1, §2) has no Docker, no Redis, and no asyncio anywhere — and there are *two* Python processes needing to reach the agent (the API for `health`/`resolve_group`, the worker for `send_text`), not one. Inventing a signed RPC protocol for that shape would be the only asynchronous, network-listening thing in an otherwise 100%-synchronous codebase. Instead, `LocalAgentProvider` writes a command row and polls for its result (fresh session per poll, matching `workers/loop.py`'s own "every tick opens a fresh session" idiom) — every method on the port stays synchronous and blocking exactly as designed, and `scheduler_service.py` needed **zero changes**. The agent claims work with `FOR UPDATE SKIP LOCKED`, the same pattern `scheduler_repository.py` already uses. What's lost versus the signed-envelope design: per-command authentication. What's gained: the `interlock_agent` role is scoped to exactly two tables and can't reach `tasks`, `report_snapshots`, or `audit_logs` at all — arguably a *narrower* blast radius than a WSS client authenticated against the whole API would have had.

### Decisions worth knowing

- **A command row only reaches `DONE` for a terminal outcome** (success, or a failure retrying can't fix). A poll timeout in `LocalAgentProvider` is always reported as `ErrorClass.TRANSIENT`, **never `AMBIGUOUS`** — there is no code path that can return it. This means the pre-existing gap (`scheduler_service.py` never calls `delivery_state()`, so `AMBIGUOUS` has always been dead-letter equivalent to `PERMANENT`) is neither fixed nor worsened here; it just never gets exercised by this provider. A TODO in `domain/ports/whatsapp.py` near `ErrorClass.AMBIGUOUS` points back here.
- **`send_text`'s idempotency pre-check mirrors `MockWhatsAppProvider` exactly**: "was a `DONE` result already known when this call *started*", not "did my insert hit a conflict" — the latter gives the wrong answer for the reconnect case (see `test_offline_then_online_sends_exactly_once`, which traces through why).
- **Commands expire.** Every insert (and every retry — the ON CONFLICT DO UPDATE only fires on a still-`PENDING` row) sets/refreshes `expires_at`; the agent's `claim_next` never claims a row too close to or past it. Without this, a row Python already reported `FAILED` could still send later once the agent came back online.
- **The WhatsApp message id is persisted before the agent sends, not after** (`set_wa_message_id`, called before `sock.sendMessage`). A crash-and-restart resend reuses it, so WhatsApp's own dedup (not this app's) absorbs a genuine mid-flight crash. Never verified against a real account — flagged explicitly in the setup doc's manual test checklist.
- **`WHATSAPP_LOCAL_AGENT_TOS_ACK` is a second, deliberate gate** beyond `WHATSAPP_PROVIDER=local_agent` itself, ships `false`/commented out, and `build_whatsapp_provider` raises a `RuntimeError` naming the ToS risk if it's missing — this is about knowing legal/ban risk, not just a missing credential, so it gets more friction than `SHEETS_PROVIDER=google`'s missing-credential check does.
- **`interlock_agent` is provisioned by its own script**, not folded into `setup-database.ps1` or granted inside the migration — the role is optional, and a `GRANT ... TO interlock_agent` baked into the migration would fail on every database that doesn't have it, `interlock_test` included, breaking the whole suite.

### Baileys API details worth knowing if this breaks

`@whiskeysockets/baileys` (pinned `7.0.0-rc14` at time of writing — a release candidate; the `latest` npm dist-tag, not `legacy`) is ESM-only, while the rest of `apps/agent` is CommonJS (pg, pino, dotenv all still are). Rather than converting the whole project to ESM, `baileys.ts` loads Baileys' runtime values (`makeWASocket`, `DisconnectReason`, `useMultiFileAuthState`) via a cached dynamic `import()`, and every *type* reference uses `import type ... with { "resolution-mode": "import" }` — TS 7's required syntax for a type-only import of an ESM package from a CJS file. If a future TypeScript/Node upgrade changes this interop story, this is the one file affected.

Baileys' ack levels (`proto.WebMessageInfo.Status`: `ERROR=0, PENDING=1, SERVER_ACK=2, DELIVERY_ACK=3, READ=4, PLAYED=5`) are mapped to the port's `DeliveryAck` in `baileys.ts::ackToDeliveryAck` — verified against the installed package's own type definitions, not guessed.

### Known gaps — deliberately NOT fixed

- **No push alerting.** `AUTOMATION_ERROR`/stale-heartbeat visibility is pull-only (`/whatsapp/status`, `/readiness`, the agent's log file) — same already-accepted gap as notification delivery generally (§8).
- **`whatsapp_agent_commands` has no retention/cleanup job.** Grows unbounded, harmlessly at this message volume; a manual `DELETE ... WHERE status='DONE'` is the documented fallback.
- **Rate limiting and the circuit breaker are in-memory, not persisted** — both reset on every agent restart. Restarting does not evade WhatsApp's own server-side throttling, only this app's self-imposed one.
- **Sends are serialized through one agent process**, one at a time — a job with many recipients could make one worker tick take a while in the worst case. Fine at this tool's real scale; flagged as a known, accepted characteristic.
- **The canary's own success/failure doesn't update `whatsapp_agent_status.last_canary_at`/`last_canary_ok`** yet — it's visible via the `whatsapp_agent_commands` row itself (`op='test_send'`), but those two status columns stay `NULL`. Wiring them up is a small, separable follow-up inside `BaileysAgent`'s ack handler.
- **`AgentDb.enqueueTestSend` is a raw INSERT**, the one write in the Node agent that doesn't go through `agent_queries.sql` — enqueueing was scoped as Python's job in the shared contract; the canary enqueueing itself for its own use is the one deliberate exception.

### Tests

Python: `tests/unit/test_local_agent_mapping.py`, `test_whatsapp_factory.py`; `tests/integration/test_whatsapp_agent_repository.py`, `test_local_agent_provider.py` (a `FakeAgent` thread running the *actual* `agent_queries.sql`, mirroring every behavior `test_mock_provider.py` pins — replay idempotency, offline-then-reconnect, permanent-failure memoisation, a hung claim never becoming `AMBIGUOUS`, transient release reusing the WhatsApp id, expiry, `resolve_group` timeout, `health()` never raising even with the database unreachable). Node: `apps/agent/test/*.test.ts` — `sqlNamedQueries` (including a test that the real `agent_queries.sql` file parses to exactly the six statements the agent expects), `rateLimiter`, `circuitBreaker`, `commands` (group-ranking, matching `MockWhatsAppProvider`'s rules case for case), `baileys` (ack mapping only — the socket itself is untestable here, see above).

**Note for `test_local_agent_provider.py` specifically**: uses a real `SystemClock`, not `FrozenClock` — expiry and heartbeat staleness are judged against Postgres's own `now()`, so an arbitrary frozen historical instant would already read as "expired" to the database. If you add a test here, don't reach for `FrozenClock` out of habit.

### First-run bugs found and fixed before the agent ever ran for real (don't reintroduce)

Compiled output runs from `apps/agent/dist/src/`, so `__dirname/..` is `dist/`, not `apps/agent/`. Three paths had this wrong:
- `db.ts`'s path to `sql/agent_queries.sql`. This would have crashed on startup.
- `baileys.ts`'s `.wa-session/`. The pairing would have lived under `dist/` and been wiped by a clean rebuild.
- `logger.ts`'s `logs/`.

All three now go up two levels. `test/db.test.ts` constructs a real `AgentDb` (pg's `Pool` is lazy, so no database is needed) to pin the SQL path.

Separately, the file logger was async, so a startup failure's `process.exit()` lost the one line explaining it, and pino then threw "sonic boom is not ready yet". It is now `sync: true`, and it also echoes to stdout when run from a real terminal.

The setup doc's Scheduled Task pointed at `dist\index.js`; the entry point is `dist\src\index.js`.

The dynamic `import()` of Baileys was confirmed preserved in the compiled CJS, and confirmed to resolve at runtime on Node 22.11.

**Found on the first real run (30 Sep 2026), now fixed:**
- **Every claim failed with `missing parameter "interval"`.** `sqlNamedQueries.ts`'s placeholder regex `:(\w+)` also matched Postgres casts (`)::interval`). Comment lines were also sent as part of each statement, and `complete`'s comment mentions `::jsonb`, so the first completed send would have failed the same way. The regex is now `(?<!:):(\w+)`, and full-line `--` comments are dropped per block. `test/sqlNamedQueries.test.ts` now converts **every real statement** with exactly the params `db.ts` passes. The old test only checked that the file split into six blocks, which is why this got through.
- **Ctrl+C never exited.** `pool.end()` waits for every checked-out client, and the LISTEN client was never released, so shutdown hung while the loop kept polling. On top of that, Baileys' close handler reconnected and printed a new QR code. Now `AgentDb.close()` releases the LISTEN client first, `BaileysAgent.close()` ends the socket without reconnecting (the pairing is kept), the loop stops, and a 3 s timer forces the exit regardless.
- "QR refs attempts ended" (status 408) followed by a fresh QR is normal Baileys behaviour when a code isn't scanned in time. It isn't a bug.

---

## 12. Read-only sheet import — the task source actually in use

**Status:** built, and **verified against the user's real sheet**. `scripts/sheet_import_preview.py` ran read-only; then a real tick against the dev DB created 96 tasks, a second tick changed nothing, and the rendered evening report was correct. Setup: `docs/sheet-import-setup.md`. Plan: `C:\Users\Admin\.claude\plans\velvety-whistling-kernighan.md`.

**Model:** one-way, read-only. Every `SHEET_SYNC_INTERVAL_SECONDS` the worker:
1. fetches the sheet's public CSV export;
2. parses it (`domain/sync/sheet_import.py`, pure);
3. mirrors each row into `tasks` (`services/sheet_import_service.py`), matched by `Sr No.` stored in `Task.external_row_ref` as `sr:<n>`, with `source = SHEET_IMPORT`.

Nothing is ever written to the sheet. It shares the worker's sheet slot with the two-way sync (§10) and is **mutually exclusive** with it: `build_sheet_import_fetch` raises if both are configured.

| Piece | Where |
|---|---|
| Parsing: headers, mixed date formats, status map, skips | `domain/sync/sheet_import.py` |
| Fetch + link→export URL + "went private" guard | `adapters/sheets/csv_source.py` |
| Factory + mutual exclusion | `adapters/sheets/factory.py::build_sheet_import_fetch` |
| The tick | `services/sheet_import_service.py` |
| Read-only guard | `services/task_service.py::update` (refuses `SHEET_IMPORT` tasks) |
| Worker wiring | `workers/loop.py::run_one_tick(sheet_import=...)` |
| Migration | `d3b8e61a0f47` (adds `SHEET_IMPORT` to `ck_tasks_source`) |
| Read-only preview | `scripts/sheet_import_preview.py` |

### Decisions worth knowing

- **The sheet owns imported tasks.** `TaskService.update` refuses to edit them ("edit it in your Google Sheet"), which also covers `/commit`'s `task_updates`. The alternative, allowing the edit, would see it silently reverted within a minute. Tasks created in the app stay editable, and the import never touches them.
- **History keeps its own dates.** On creation:
  - `created_at` and `updated_at` are the row's `Date`, as the start of that day, clamped to never be in the future;
  - rows already closed get `completed_at` = that `Date` too.
  
  Without this, the first import would report ~90 tasks as "added today". Later transitions use real time; `apply_edit` stamps or clears `completed_at`.
- **Delegated rows** (`<Name>'s Task` / `<Name> Task`, including the phone's curly apostrophe) **are hidden with the existing soft delete** (`deleted_at` plus a `delegated:<name>` tag). Every summary, report and default task listing already skips deleted tasks, so no report logic changed. They're un-hidden if the status changes back. The importer reads with `include_deleted=True`.
- **Never guess:**
  - blank or duplicate `Sr No.` → skipped (every copy of a duplicate) and logged;
  - blank `Task` → skipped;
  - unrecognised status → Pending for a new row, current status kept for an existing one, logged either way;
  - a row that vanishes from the sheet → its task is left alone;
  - a missing required header → the whole tick refuses to act.
- **Mixed date formats** (the sheet really does have both `29-04-2026` and `05-25-2026`): read month-first; if the first number is above 12, read day-first; two-digit years mean 20xx; a missing year means this year; an unreadable date becomes `None`.
- **A sheet that goes private returns HTTP 200 with an HTML sign-in page.** `fetch_csv` rejects any response that isn't `text/csv` or that starts with `<`, so this can never look like an empty sheet. It uses stdlib `urllib`, which follows Google's 307 redirect; `httpx` is dev-only in `pyproject.toml`.

### Report behaviour changed globally (not just for imported tasks)

- `build_context` now lists **open tasks plus tasks completed today**. Tasks completed on earlier days are never listed, and completed-today tasks sort last.
- `TaskSummary` gained `completed_today` (also in `TaskSummaryOut`, `ALLOWED_VARIABLES` and `docs/openapi.json`). Both default templates now show "Completed today" instead of the all-time `completed` count, which still exists.
- **A pre-existing Phase 1 bug was fixed here:** every task line ended in `{% if task.overdue %}…{% endif %}`, and `trim_blocks` swallowed the newline after the `{% endif %}`. Every rendered report had its task lines glued together ("In Progress2. Next task"). The fix uses an inline `{{ " — overdue" if task.overdue else "" }}`, and the regression is pinned by `test_every_task_starts_on_its_own_line`.

### Known gaps — deliberately NOT fixed

- Only one tab is read (from `#gid=`). The sheet's other tabs (Sheet6, Project Manager, Project, Sheet5) are ignored.
- Renumbering an existing row's `Sr No.` looks like a new row plus a vanished one. You'd get a duplicate task; the old one is left, not deleted.
- The sheet has no priority column, so everything imports as MEDIUM, and report ordering is overdue-first, then display id.
- Delegation uses `deleted_at` for "hidden". If a real delete feature is ever added, those two meanings must be separated.

### Tests

- Unit: `tests/unit/test_sheet_import_parsing.py` (the fixture mirrors the real sheet's quirks), `test_csv_source.py`, `test_sheet_import_factory.py`; new cases in `test_render_and_snapshot.py`.
- Integration: `tests/integration/test_sheet_import_service.py` — historic timestamps, idempotence, edits audited, the Closed transition, hide/unhide, vanished rows, skips, a failed fetch changing nothing, and the read-only guard.

---

## 13. Review / approve UI

**Status:** built 30 Sep 2026. The user chose **Vite + React** over Phase 0's Next.js: it's served by the API at `/app/`, so there's no extra process and no CORS. Usage and build: `docs/web-ui.md`.

| Piece | Where |
|---|---|
| App | `apps/web/src/`: `App.tsx` (shell, WhatsApp status pill), `pages/{Dashboard,Review,Groups}.tsx`, `logic.ts` (pure, tested), `api.ts` (typed client) |
| Types | `src/api-types.ts`, generated from `docs/openapi.json` (`npm run gen:api`); never hand-edit it |
| Serving | `api/routers/ui.py`: `GET /config/ui`, static mount at `/app`, and `/` redirects to `/app/` (or `/docs` if not built) |
| New endpoints | `GET /approval-requests` (list, `local_date`, `limit`), `POST /approval-requests` (MANUAL review), `shares[]` on `GET /approval-requests/{id}`, `content_hash` on `/preview`, `expected_content_hash` on `/commit` |
| Tests | `tests/acceptance/test_review_ui_endpoints.py` (14), `apps/web/test/logic.test.ts` (18) |

### Decisions worth knowing

- **Preview/commit drift guard.** `/preview` returns `compute_content_hash(tasks)`, the same fingerprint snapshots use. `/commit` with `expected_content_hash` raises `DatasetVersionConflictError` (409) if the data moved. It's checked *before* the commit's own task edits and only for share modes. The rendered body can't be compared because it contains `{{ time }}`. The UI auto-refreshes the preview every 60 s, flags "Your tasks changed" when the hash moves, and re-previews on a 409.
- **No task editing in the UI.** Tasks are sheet-owned (§12). The UI only sends `SHARE_ONLY`, or `UPDATE_ONLY` with no edits ("Don't share this one", which ends in `CLOSED_NO_SHARE`).
- `action_version = shares.length + 1`, with a fresh `Idempotency-Key` per click. The backend's `(review, action_version)` uniqueness makes a double click send once.
- Recipients preselect from `default_morning`, or from `default_evening` for EVENING and MANUAL reports (the same reasoning as `default_template_for`).
- All times display in `Settings.timezone`, not the browser's zone (`logic.ts`). "Later today at" converts that wall time to UTC for `send_at`.
- Hash routing (`#/review/<id>`), so a refresh never 404s against the static mount.
- Desktop alerts use the browser Notification API: only while the tab is open, and never for reviews that already existed at page load.
- **Pinned for Node 22.11:** Vite 6.4, plugin-react 4.7, TypeScript 5.9 (openapi-typescript needs TS 5), vitest 4.1.11 (3.x had a moderate advisory). `npm audit` is clean.

### Known gaps, deliberately not fixed

- A group's "preselect" flags can only be set when it's pinned; there's no endpoint to change them later.
- Nothing in the UI resolves Sheets-sync conflicts (`/sync/conflicts`). Two-way sync is unused (§10).
- There's no "share again" after a report is approved. The API allows a new `action_version`, but the UI shows delivery only.
- Checked via headless Edge screenshots at 1100px and 520px wide, in dark theme. It hasn't been clicked through in a real browser yet.

### Image-format reports (30 Sep 2026)

The user found the emoji text report unprofessional. Reports now go out as a **sheet-style table image plus a one-line caption** (`REPORT_FORMAT=image`, the default; `text` keeps the old Jinja templates). There's deliberately **no colour coding**. Overdue is kept at the user's request, stated in words in the Deadline cell ("25 Sep 2026 (overdue)") and in the caption count.

- **Content, pure:** `domain/sharing/table.py` has `build_report_table` and `render_caption`. It uses the same task selection as the text report (`render.select_report_tasks`). Columns are No. (the sheet's Sr No.), Task, Status, then Deadline and Note only when used. It caps at 40 rows and says "N more not shown".
- **Drawing:** `adapters/rendering/table_image.py`, Pillow (a new dependency), Segoe UI falling back to Arial and then DejaVu. It's behind the `domain/ports/rendering.py` port and built once in `create_app`.
- **Frozen:** `report_snapshots.rendered_image` (bytea, migration `e5c2a7d9b418`) is rendered at approval. The sender sends those exact bytes, and the acceptance test checks the sent PNG equals the served one.
- **Send path:** `WhatsAppProvider.send_text(..., image_png=None)`. `LocalAgentProvider` adds `image_b64` to the outbox payload, and the agent sends `{image, caption: body}`.
- **API:** `has_image` on `/preview`; `GET /approval-requests/{id}/preview.png` (live); `snapshot_id`/`has_image` on each share; `GET /shares/snapshots/{id}/image.png` (frozen). The UI shows the image in both the preview and delivery panels.
- **Tests:** the nine scenarios pin `REPORT_FORMAT=text` in `tests/acceptance/conftest.py`. The image format has `tests/unit/test_report_table.py` and `tests/acceptance/test_image_reports.py`.
- `test_whatsapp_factory.py::test_defaults_to_the_mock` now ignores the developer's `.env` (the real `.env` has `WHATSAPP_PROVIDER=local_agent`).
- **Found while deploying:** two agent processes were running at once, one started by hand and one from the launcher. Exactly one must run. `start-interlock.ps1 -NoAgent` exists for when the agent is already up.
- **Group sends never completed, found on the first real image sends (30 Sep 2026, now fixed).** For **group** chats, Baileys 7 never emits a `messages.update` status. The server's success ack produces no event at all, and group delivery receipts arrive per member on `message-receipt.update` (see `handleReceipt` in Baileys' `lib/Socket/messages-recv.js`). The agent waited only on `messages.update`, so every group send reached WhatsApp but stayed `CLAIMED`. Python timed out after 20 s, retried 4×, and marked the recipient `FAILED`. The same `client_message_id` meant those retries did **not** resend, but the user approved new reports four times thinking it had failed, so the groups got duplicates. Now `BaileysAgent` completes a send on whichever comes first: a `message-receipt.update` (DEVICE), a `messages.update` SERVER/DEVICE/READ, or `SERVER_ACCEPT_WINDOW_MS` (3 s) after `sendMessage` resolves with no ERROR. An ERROR inside that window still releases the row for retry. **Consequence:** the 5 jobs / 10 recipients from 30 Sep 12:50–13:00 are recorded `FAILED` but were actually delivered. Don't press Retry on them (a manual retry mints a new id and *would* resend).

---

## 14. Refinement round (1 Oct 2026): popup, WhatsApp sessions, UI, installer

The user asked for four refinements without rewriting anything that works: a cleaner Reports UI, a 09:00/17:00 popup with send options, WhatsApp reconnect from the UI (QR), and a one-command installer. Plan file: `C:\Users\Admin\.claude\plans\pasted-content-id-2df4-bismillah-proud-biscuit.md` (read it: it has the full design for M3–M5, including the WhatsApp session table).

### M1 — scheduling correctness (done, commit `9d55b1f`)

**Bug found while planning:** `validity.py` measured the grace from *approval*, so "approve at 17:00, send at 21:30" was held back as stale. Scenario 5 only passed because its test clock committed at 22:30, after 21:30 (the §6.5 clock-desync trap).
- `evaluate_send_validity(..., run_at=)` now measures both the day boundary and the grace from the **intended** send time, `intended_send_time() = max(approval, run_at)`. Immediate sends behave as before. Scenario 5 now commits at 17:00.
- While WhatsApp can't send (`health().can_send` is false, read once per tick), due actions are handed back with `reschedule(..., note=WAITING_FOR_WHATSAPP)` and due retries are skipped: **no attempts burned**. On reconnect they send if still in time; otherwise they're held back with the new `DeferReason.WHATSAPP_DISCONNECTED` (migration `f1a9c3d7e052`).
- An automatic **retry** whose report's day has ended fails as `MISSED_DAY` instead of delivering yesterday's report (`missed_its_day`). A human pressing Retry is deliberately not held to it.
- Tests: `tests/acceptance/test_send_later_and_outage.py`, `TestScheduledForLater` in `test_validity.py`.

### M2 — the popup service (done)

- **Derived, not stored:** the popup is a pure function of the open review in Postgres (`domain/approvals/prompt.py::pick_prompt`): today's **latest** scheduled review if it's open (`REVIEW_PENDING`/`USER_EDITING`/`READY`) and not snoozed. There's no notification table to lose or duplicate; a restart can't lose it, and a second tab can't show it twice. A handled evening review does not resurrect the ignored morning one. MANUAL reviews never prompt.
- **Snooze** = `approval_requests.snoozed_until` (migration `a7d3e5b91c64`), orthogonal to the approval state machine. `ReviewService.snooze` (1–240 min, open reviews only, audited as `REVIEW_SNOOZED`); `POST /approval-requests/{id}/snooze`; `GET /notifications/prompt`.
- **Actions reuse `/commit`**: Send now, In 5 minutes, Custom date+time (≤7 days ahead), and Skip (`UPDATE_ONLY`). "In 5 minutes" is sent as `delay_minutes`, which the **server** counts from its own clock (mutually exclusive with `send_at`); a browser clock can't skew it.
- **Web:** `components/PromptHost.tsx` (app-level; polls every 20 s and on focus; hidden on that review's own page; desktop notification + tab-title marker when the tab is in the background; closing with × means Snooze 30), `components/Modal.tsx`, `components/ReportPreview.tsx`. `useShare.ts` holds the shared share logic (groups, preview polling, hash guard, commit): **both the popup and the report page use it**.
- Latency: the worker creates the review on its tick (`SCHEDULER_TICK_SECONDS`, 30 s), then the page polls, so the popup appears within about a minute of 09:00/17:00.
- Tests: `test_prompt.py` (unit), `test_prompt_and_snooze.py` (incl. restart survival), web `logic.test.ts` (27).

**How it was verified in a real browser** (repeat this for UI work): a disposable API on `interlock_test` with `WHATSAPP_PROVIDER=mock` and a **file-controlled warp clock**, plus a ~100-line CDP driver over headless Edge (Node 22's built-in WebSocket) that seeds data, really clicks, and checks server state. It covered: popup appears by itself, hidden on the report's own page, Send now, 5 min, custom tomorrow 09:00, skip, × = snooze, snooze → quiet through a reload → returns by itself after the clock is advanced 31 min, phone width. The scripts lived in the session scratchpad (not in the repo); recreate them if needed (the wiring is: `create_app(settings, clock=WarpClock(), whatsapp_provider=Mock...)`).

### M3 — WhatsApp session management (done, verified on real WhatsApp)

**Design (kept from the existing agent, nothing rewritten):** one agent process owns the session; the UI controls it through the existing Postgres outbox. New ops `link_start` / `link_cancel` / `reconnect`; progress and the QR come back through `whatsapp_agent_status` (migration `c3b7e1f94a26`: `status_reason`, `account_jid/name`, `pairing_*`).

- **Never loop, never print stray QRs** (`apps/agent/src/disconnect.ts`, pure and tested): a dropped connection reconnects with back-off (2 s → 60 s); 401/500/411 means the session is dead, so it is **retired** (the pointer is cleared; the folder is left on disk untouched and tidied later) and the state is `LOGIN_REQUIRED` with reason `LOGGED_OUT` / `SESSION_INVALID`; 440 (taken over) and 403 (refused) halt with reasons `REPLACED` / `FORBIDDEN`; 515 (after a scan) restarts at once. With **no completed pairing on disk no socket is opened at all**, so there is no QR in the background (`NOT_LINKED`). A QR on the *main* socket means the session is invalid, and it halts.
- **Sessions are folders that are never renamed** (`sessionFiles.ts::SessionStore`): `.wa-session/` is the original folder, used until the first UI link; every phone linked from the UI gets its own `.wa-sessions/s-<time>-<id>/`, and `.wa-sessions/active.json` is an atomic pointer naming the live one (`{"dir": null}` = none). "Switching" is a pointer write. `activeDir()` survives a damaged pointer (newest complete session wins). Old folders are tidied by `prune()` (keeps the live one and the newest other), strictly best-effort.
- **Linking and switching** (`pairing.ts`, `linkController.ts`, `handover.ts`): each attempt links into a fresh numbered folder, so the live connection and scheduled sends keep working while a QR is on screen. `performHandover()` then: waits for that folder to go quiet (`waitForQuiet`: the socket that linked it is still flushing files) → opens the new session and waits for WhatsApp to accept it (`awaitOpen`) → activates it (pointer) → starts using it → and **last, best-effort**, logs the old device out. Any failure before activation leaves the old link exactly as it was. Cancel / expiry / failure change nothing, and a failed switch does **not** delete the new session (it may be fine).
- **Single instance:** a Postgres advisory lock, taken before `reset_stale_claims` (which would otherwise steal a live peer's work). A second agent exits with code 3. Not connected ⇒ the loop claims only control ops (`claim_next_control`), so a send or lookup it can't do is never left stuck CLAIMED.
- **Python:** `WhatsAppLinking` is a separate port from `WhatsAppProvider` (`domain/ports/whatsapp.py`, plus `LinkStatus`, `PairingState`, `phone_from_jid`); `LocalAgentProvider` and the mock implement it. A failed group lookup now raises a clear error instead of a 20 s timeout, and `/whatsapp/groups/resolve` fails fast when disconnected.
- **API:** `GET/POST/DELETE /whatsapp/link`, `GET /whatsapp/link/qr.svg` (segno; dark on an explicit white ground, since a transparent QR is unscannable in dark mode; `Cache-Control: no-store`), `POST /whatsapp/reconnect`; `/whatsapp/status` gained `reason`, `account_number`, `account_name`.
- **Web:** `WhatsAppBanner` (page-wide, per-reason wording in `logic.ts::whatsappBanner`), `WhatsAppLinkModal` (confirm-before-changing-phone, QR that refreshes itself, scanned / success / expired / failed / service-stopped), a **Settings** page with the WhatsApp card, and a pill that says "service stopped" (`AGENT_OFFLINE`) separately from "needs linking". The banner is silent for the mock provider and for a brief automatic reconnect.
- **Tests:** agent 71 (pure logic, fake sockets, real temp dirs), `test_whatsapp_linking.py` (against the agent's own SQL), `test_whatsapp_link_api.py`, web 39. Real-browser run of the six reconnect scenarios (logged out, expired QR, change phone, taken over, service stopped, phone width) against a scripted provider named `e2e-fake` (the name `mock` is deliberately treated as test mode by the UI).

**Verified live against the user's real WhatsApp (1 Oct 2026):** the new agent takes the lock, reconnects to the existing session in ~2 s and reports `+918910056457`; `POST /whatsapp/link` produces a real QR within 2 s, WhatsApp rotates it after ~60 s, the live connection stays CONNECTED throughout, and cancel leaves no scratch files.
**The first real phone test FAILED, and was instructive (1 Oct 2026):** the first version of the switch-over logged the old device out, then *renamed* the freshly linked session folder into place. Baileys was still writing ~1,200 files into it, Windows refused (`EPERM`), the rollback restored the old (now logged-out) session, and then the clean-up `rmSync` of the new folder failed half-way (`ENOTEMPTY`) *after deleting `creds.json`*. Result: WhatsApp showed the new device as linked but Interlock had lost its credentials, the agent still claimed CONNECTED with no socket (stale state), and the user needed another scan. Fixed by the design above (no renames; prove the new session before retiring the old; clean-up can never throw or delete a session) with regression tests for each step.
**Verified live after the fix:** logged-out detection on the real thing (the agent opened the dead old session, got 401, stopped cleanly in ~6 s and showed the banner), then a full relink from the banner: scan → link → `performHandover` → `adopt.switched`, connected as `+918910056457` (device `:35`), no errors, nothing renamed.
**Then verified live, all on real WhatsApp:** *Link a different phone* while connected, three times: main phone → a second number (`+917980381503`, in the "Test" group but **not** the two work groups) → back to the main phone (`+918910056457`, in all three). Each time the new session was proven before it became active, the old device was logged out last, and `prune()` removed the dead folders (no `EPERM`/`ENOTEMPTY`, no errors). The user scanned with a different number on purpose or by chance, which happens to be the realistic "new number isn't in the groups" case: `POST /whatsapp/groups/resolve` for each pinned group is a cheap read-only check of membership.
**Not done live (optional):** removing the Interlock device from the phone *while connected* (banner within ~15 s, then Reconnect). The same 401 handler was exercised for real at connect time; the runtime path differs only in how Baileys delivers the same close code. The user should also remove the orphan entry the failed first attempt left in their main phone's Linked Devices.

**Things learnt (don't repeat):**
- **Never rename or delete a folder Baileys has recently written to on Windows.** Address sessions in place (pointer), wait for quiet, and treat deletion as optional tidying. Test the real thing on a real phone before trusting a session-handling design; the fakes could not have shown this.
- `.gitignore` covered only `.wa-session/`; the agent's session folders hold credentials, so it is now `apps/agent/.wa-session*/` (this also covers `.wa-sessions/`).
- The agent used to keep reporting CONNECTED after it had closed its own socket; any path that closes the live socket must set an accurate state (the new design never leaves a socket-less CONNECTED).
- A real `setTimeout` inside pairing made each agent test file linger ~3 minutes (and race). Timers are injectable (`Timers`) and the real one is `unref()`'d. The agent suite takes ~1 s.
- Windows paths with backslashes in inline `python -` heredocs get mangled (`\a` became a BEL character). Use the Write/Edit tools for any text containing backslashes, and scan for control characters.

### Background service (done 1 Oct 2026, ahead of M4, at the user's request)

One hidden **supervisor** process runs the API, the worker and the agent, so nothing needs a window and nothing stays dead. Verified on this laptop with the real stack: started, WhatsApp reconnected as `+918910056457`, `-Restart` and `-Stop` clean (one agent, old pids gone), and the logon task fired on demand without a duplicate.

| Piece | Where |
|---|---|
| The supervisor (`python -m interlock.supervisor`) | `src/interlock/supervisor.py` |
| Controller: start / `-Stop` / `-Restart` / `-Status` / `-Console` | `scripts/start-interlock.ps1` |
| Start at login (Scheduled Task "Interlock", Startup-folder fallback; `-Remove`) | `scripts/register-autostart.ps1` |
| Tests (24: restarts, logs, stop, grandchildren, locks, leftover sweep) | `tests/unit/test_supervisor.py` |

**Decisions worth knowing:**
- **Restarts back off** 2 s → 60 s and reset after a 60 s healthy run (`Backoff`, pure). A child that can't even start (node missing) is retried the same way.
- **Children are stopped with `taskkill /T /F`, not `Popen.terminate()`.** The venv's `python.exe` is a *launcher* that starts the real interpreter as its child; stopping only the launcher leaves the real one running (it keeps the API port). A test pins this.
- **One supervisor at a time** through an OS file lock (`logs/supervisor.lock`, released if the process dies, so no stale lock). A second start exits **0**, not an error, so the logon task doesn't retry for nothing.
- **Stop = a stop file** (`logs/supervisor.stop`): a hidden process has no window to close. `-Stop` waits 30 s, then ends the supervisor.
- **Hard kill:** the supervisor joins a kill-on-close Windows job object, but that is **best effort and cannot be proven in a Node/Electron-launched shell**: those run inside a job with silent breakaway, so children never join ours (this was probed and confirmed). The dependable guarantee is the **start-up sweep**: the status file records each child's pid *and process creation time*, and the next start stops exactly those still running (a recycled pid never matches). Don't "fix" the failing-looking job behaviour in a harness; test the sweep.
- **Everything logs to files** (`logs/`, rotated at ~2 MB × 3). Under `pythonw` there is no console, so anything that goes wrong must reach `supervisor.log`; an uncaught crash is logged there. The first start failed silently on a 64-bit `ctypes` handle overflow (`AssignProcessToJobObject`) and left only an empty lock file: that is why.
- **Not done, deliberately:** a watchdog that restarts a *running but hung* agent (its heartbeat goes stale while the process is alive). Process-level supervision covers the crash case; add the watchdog only if a hung agent is actually seen.
- The old per-agent "Interlock WhatsApp Agent" Scheduled Task from `docs/whatsapp-agent-setup.md` is replaced (the doc is rewritten; `register-autostart.ps1` removes that task if it exists).

### M4 front end (done 1 Oct 2026)

The Reports page, the report page, the design system and the Settings cards, without rewriting what worked: the popup, the WhatsApp banner and link dialog, Groups and the data hooks (`useShare`) are the same code, restyled by the shared stylesheet.

| Piece | Where |
|---|---|
| Result wording, filters, periods, "next report in…", report banner (all pure) | `apps/web/src/logic.ts` (`deliveryResult`, `matchesFilter`, `periodStart`, `nextReport`, `untilLabel`, `reportBanner`, ...), tests in `test/logic.test.ts` (65) |
| Reports page: status strip, Needs-your-approval cards, filter tabs, period switch, table (cards on phones) | `pages/Dashboard.tsx` |
| Report page: status banner, message left, sticky action panel right; after approval, the frozen message and per-group delivery | `pages/Review.tsx` |
| Settings: WhatsApp, Google Sheet, Schedule | `pages/Settings.tsx` |
| Icons, icon+text chips, callout | `ui.tsx` |
| Toast, confirm dialog, image lightbox, WhatsApp pill | `components/{Toast,ConfirmDialog,ReportPreview,WhatsAppPill}.tsx` |
| Close a waiting report (the existing UPDATE_ONLY commit) | `useCloseReport.ts` |
| Tokens (spacing/type/radii scales), buttons, chips, table, responsive rules | `styles.css` |

**Decisions worth knowing:**
- **One fetch, filtered in the browser.** The Reports page fetches the last 30 days once (`since`, limit 100) and the Today / 7 days / 30 days switch and the filter tabs filter locally, so switching is instant. The Needs-your-approval cards come from the same list, so an open report is never hidden by the period.
- **The words are built client-side** (`deliveryResult`) from the server's numbers plus the time and the WhatsApp state. A send due within a minute counts as "Sending", not "Scheduled" (the page's clock refreshes every 30 s). A due send while a *real* provider is down reads "Waiting for WhatsApp"; the mock is never "down".
- **Every status is icon + words**, never colour alone (`Chip`).
- **Closing a report asks first** (`ConfirmDialog`, safe choice focused), from the card and from the report page. The popup's "Skip this report" still uses `window.confirm` (a modal inside the popup would fight over focus).
- **The popup is unchanged** and still hidden on its own report's page.
- `.check input` is now limited to checkboxes and radios: the generic rule had shrunk the "Later today at" time input to a dot.

**How it was verified (repeat this for UI work):** a disposable API on `interlock_test` with a fake *connected* WhatsApp (provider name `e2e-fake`, not `mock`, which the UI treats as test mode), seeded with a week of reports in every state (sent, partly sent, failed, held back, not shared, scheduled, waiting), plus a ~100-line Chrome DevTools driver over headless Edge (Node 22's built-in `WebSocket`) that really clicks and checks: filters (Problems 3, Sent 4), periods (Today 2), the close dialog, the image lightbox and Escape, a real share, light and dark theme, and a true 390 px phone viewport (`Emulation.setDeviceMetricsOverride`, since headless Edge refuses a window narrower than about 500 px) with no horizontal scroll and no console errors: 21/21. The scripts live in the session scratchpad, not the repo; recreate them if needed (`create_app(settings, clock, whatsapp_provider, sheets_provider)` on the test DB; seed through `TestClient` with a `FrozenClock` stepped through past days, using `tests/acceptance/conftest.py`'s `trigger_reviews` / `run_tick`).
**Not covered:** a click-through of the Groups page (unchanged, restyled only), and light theme was checked on the Reports page only.

### M4 backend (done 1 Oct 2026)

- `GET /approval-requests` now returns `ReviewListItemOut`: the review plus `delivery` (`DeliverySummaryOut`: `job_state`, `run_at`, `sent_at`, `deferred_reason`, `total/sent/failed/pending/skipped`, sorted `group_names`) or null when it was never shared. **The latest job per review** (highest `action_version`) is summarised.
- `ShareRepository.delivery_summaries(review_ids)` does it in a fixed four queries however many reviews are listed; the pure counting is `domain/sharing/summary.py`.
- New `since` (date) query parameter: reviews for that day or later. The Reports page's Today / 7 days / 30 days will send it.
- `/config/ui` gained `working_days` (Monday = 0) for the Settings Schedule card.
- Tests: `tests/acceptance/test_reports_list.py` (9). `docs/openapi.json` regenerated.
- The words ("Sent to 3 groups", "Partly sent 2/3", "Waiting for WhatsApp", "Held back") are **not** produced by the server on purpose: they depend on the current time and the WhatsApp connection. They belong in `apps/web/src/logic.ts::deliveryResult` (pure, tested).

### Still to do: the M5 one-command installer (`install.cmd` + `install.ps1`, winget prerequisites, database / sheet URL / WhatsApp QR steps, a UTF-8 `README.md`) — see the plan file. Its service half is already done (above).
