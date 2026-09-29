# Interlock — Handover

**Project root:** `C:\Users\Admin\Documents\interlock`
**Repo state:** git on `main`, one commit (`9cd110b`), nothing pushed. **Everything in §10–§12 is uncommitted** — ask the user whether to commit before starting new work.
**Phase status:** Phase 1 complete. Read-only sheet import (§12) **verified against the user's real sheet**. Two-way Sheets sync (§10) built but unused. Real WhatsApp (§11) built, **never run against a real account**. No frontend, no auth.

---

## 0. START HERE (for a new Claude session)

**What the user wants:** Interlock working for real, ASAP, without cutting quality. The user is Kaif. Their real tasks live in the Google Sheet **"Kaif Task List"**:
`https://docs.google.com/spreadsheets/d/1dCIwGwZbLc3G2ziy9tWok0qfLKVN4CzQU_xE7k9SSig/edit#gid=0`
It's shared "anyone with the link can view". At 09:00 and 17:00 Interlock should build a report from that sheet, let the user approve it, and send it to their WhatsApp work groups.

**Verified working end to end (29 Sep 2026):**
- The import fetched the real sheet and mirrored **96 tasks** into the dev DB. A second tick changed nothing.
- The rendered evening report listed the 7 open tasks plus the 1 closed that day.
- The dev DB (`interlock`) is at migration head `d3b8e61a0f47` and holds those 96 tasks.

**The user's own decisions — don't re-ask:**
- **Task source = read-only import from their sheet** (§12). They declined a Google Cloud service account, and they edit tasks in the sheet, not in Interlock.
- Rows with status **"Subhan's Task" / "Safwan(a)'s Task" are left out of reports entirely** (they're delegated).
- **WhatsApp via Baileys**, knowing it breaks WhatsApp's ToS (§11). The number in question is their **personal account**, which is in real work groups with their managers. Keep recommending a dedicated number, and a private test group for the first real send.

**Blocked on the user (can't be done from a tool):**
1. `.\scripts\setup-agent-role.ps1` — interactive; needs their Postgres superuser password.
2. `cd apps\agent; npm start`, then scan the QR with their phone (WhatsApp → Settings → Linked Devices). WhatsApp Desktop's session **cannot** be reused (Phase 0 risk R2).
3. `.\.venv\Scripts\python.exe scripts\whatsapp_test_send.py` — sends a message to their own number.
4. Create a WhatsApp group containing only themselves, for the first real report.

**Config the user still needs in `.env`** (not set yet — `.env` holds real credentials, never print it):
```dotenv
SHEET_IMPORT_URL=https://docs.google.com/spreadsheets/d/1dCIwGwZbLc3G2ziy9tWok0qfLKVN4CzQU_xE7k9SSig/edit#gid=0
WHATSAPP_PROVIDER=local_agent
WHATSAPP_LOCAL_AGENT_TOS_ACK=true
```

**Next work, in order:**
1. **First real end-to-end send.** Walk the user through steps 1–4, set the config, and start the worker and API (`.\.venv\Scripts\python.exe -m uvicorn interlock.api.main:app` and `-m interlock.workers.loop`). Pin the test group via `/docs` (`POST /whatsapp/groups/resolve`, then `POST /whatsapp/groups`), then approve a review via `/docs` (`POST /approval-requests/{id}/commit`).
   - Watch the risk flagged in §11: the crash-resend WhatsApp-message-id dedup has never been verified.
   - `baileys.ts` has never run against real WhatsApp, so expect to debug it here.
2. **Review/approve UI.** None exists; approving via `/docs` is the only way today. `docs/openapi.json` is the contract. This is the last piece for comfortable daily use.
3. Commit everything once the user agrees.

**How this was built — keep doing it:**
- Plan non-trivial work first (plan files live in `C:\Users\Admin\.claude\plans\`).
- Test against real Postgres, not mocks.
- Run the full battery in §7 before calling anything done.
- Every "decision" section below records *why*; read it before changing behaviour.
- The user likes short status updates and honest time estimates.

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
- **Frontend.** Nothing exists. `docs/openapi.json` is the contract to build against.
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
