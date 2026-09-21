# Interlock — Handover

**Project root:** `C:\Users\Admin\Documents\interlock`
**Repo state:** git initialized, **zero commits** — everything is untracked. Nothing has been pushed anywhere.
**Phase status:** Phase 1 ("walking skeleton") is **complete and fully verified**. Phase 2+ (Google Sheets, real WhatsApp, frontend, auth) has not been started.

Read this whole file before touching code. It tells you what exists, where, why it's built the way it is, and the specific bugs already found and fixed so you don't rediscover them the hard way.

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
│   ├── scheduling/action.py           # ScheduledAction entity, ActionStatus
│   ├── audit/chain.py                 # Hash-chain algorithm, verify_chain()
│   └── ports/                         # Protocols: TaskRepository, WhatsAppProvider, AuditSink
├── adapters/
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

# Full suite (283 tests: unit + integration + acceptance)
.\.venv\Scripts\python.exe -m pytest tests/ -v

# Domain coverage (gate: 90%, currently 96%)
.\.venv\Scripts\python.exe -m pytest tests/unit --cov=interlock.domain --cov-report=term

# Type check (must be zero errors)
.\.venv\Scripts\python.exe -m mypy src/interlock

# Lint (must be zero errors)
.\.venv\Scripts\python.exe -m ruff check src tests migrations scripts

# Re-export the OpenAPI contract after any router/schema change
.\.venv\Scripts\python.exe scripts/export_openapi.py

# Run the worker loop for real (against interlock, not interlock_test)
.\.venv\Scripts\python.exe -m interlock.workers.loop
```

**Acceptance tests specifically** (`tests/acceptance/`) drive the real FastAPI app via `TestClient` against `interlock_test`, with `FrozenClock` and `MockWhatsAppProvider` injected — no network, no real WhatsApp. They truncate the whole schema before each test (they can't use the integration suite's rollback-based isolation, since each HTTP call commits its own session, matching production). If you add a 10th scenario file, follow the pattern in any existing `test_scenario_*.py` — especially the `clock.set_to(...)` gotcha in §6.6.

---

## 8. What is explicitly NOT built (Phase 2+)

- **Google Sheets / Excel task source.** `TaskRepository` is a `Protocol` (`domain/ports/repositories.py`) specifically so this can be added later without touching `domain/` or `services/`. Only `PostgresTaskRepository` exists today.
- **Real WhatsApp delivery.** `WhatsAppProvider` is likewise a `Protocol` (`domain/ports/whatsapp.py`). Only `MockWhatsAppProvider` exists. The Phase 0 architecture doc's §8 has the full feasibility analysis (Baileys vs. official Cloud API vs. browser automation) — **read it before implementing this**, the official API cannot reach pre-existing consumer WhatsApp groups, only Baileys (unofficial, ToS-violating) can, and that's a business decision the user needs to make knowingly, not a default to silently pick.
- **Frontend.** Nothing exists. `docs/openapi.json` is the contract to build against.
- **Real authentication.** Every request currently acts as one configured default user (`DEFAULT_USER_ID`/`DEFAULT_USER_NAME` in `.env`, resolved in `api/deps.py::get_current_actor`) with optional `X-Actor-Id`/`X-Actor-Name` header overrides. This is explicitly a placeholder — see the docstring in `config.py` on those settings fields.
- **Holiday calendar**, **template editing UI**, **notification delivery** (native toast / web push) — all deferred per the Phase 0 MVP scope cut, not forgotten.

---

## 9. If you're picking this up cold, in order

1. Read the Phase 0 architecture artifact (link in §1) — at least the executive summary and §8 (WhatsApp feasibility).
2. Read `C:\Users\Admin\.claude\plans\snoopy-giggling-graham.md` for the Phase 1 scope and exit criteria.
3. Run the full verification block in §7. Confirm 283 passed, mypy/ruff clean, before changing anything.
4. Read §6 (bugs already fixed) so you don't re-spend time on them.
5. Ask the user which Phase 2 track they want first — Sheets integration, real WhatsApp, or frontend — rather than assuming.
