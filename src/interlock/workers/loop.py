"""The worker process: a durable scheduler with no broker.

Deliberately this thin. Every piece of logic that matters -- claiming due
work exactly once, evaluating whether a frozen report is still valid to send,
the per-recipient compare-and-swap, the retry ladder, the 09:00/17:00 trigger
-- lives in ``services/scheduler_service.py`` and ``services/review_trigger.py``,
already proven against a real database in ``tests/integration/``. This loop's
only job is to call those on an interval and survive being killed and
restarted, which it does simply by not holding any state of its own: every
tick opens a fresh session, and the database is what remembers everything
between ticks.

Run with: ``python -m interlock.workers.loop`` (or the ``interlock-worker``
console script from pyproject.toml).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import socket
import time
from collections.abc import Callable
from types import FrameType

import structlog
from sqlalchemy.orm import sessionmaker

from interlock.adapters.persistence.approval_repository import ApprovalRepository
from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.base import make_engine, make_session_factory
from interlock.adapters.persistence.scheduler_repository import SchedulerRepository
from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.adapters.persistence.sheet_source_repository import SheetSourceRepository
from interlock.adapters.persistence.sync_repository import SyncRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.adapters.persistence.whatsapp_group_repository import WhatsAppGroupRepository
from interlock.adapters.sheets.factory import build_sheet_reader, build_sheets_provider
from interlock.adapters.whatsapp.factory import build_whatsapp_provider
from interlock.config import Settings, SheetsProviderName, get_settings
from interlock.domain.common.clock import Clock, SystemClock
from interlock.domain.ports.sheets import SpreadsheetProvider
from interlock.domain.ports.whatsapp import WhatsAppProvider
from interlock.domain.sync.sheet_source import SheetLink, parse_sheet_link
from interlock.services.review_service import ReviewService
from interlock.services.review_trigger import maybe_create_daily_reviews
from interlock.services.scheduler_service import SchedulerService
from interlock.services.sheet_import_service import SheetImportService
from interlock.services.sheet_source_service import SheetSourceService, classify_problem
from interlock.services.sheet_sync_service import SheetSyncService

log = structlog.get_logger(__name__)

_shutdown_requested: bool = False


def _should_keep_running() -> bool:
    # A function call, not a bare `while not _shutdown_requested:` on the
    # module global directly: mypy's reachability analysis otherwise assumes
    # a global only ever mutated in a *different* function (the signal
    # handler, asynchronously, which is the entire point of a signal handler)
    # can never change within main()'s traceable control flow, and marks
    # everything after the loop as unreachable. A function call is opaque to
    # that analysis in the way it needs to be here.
    return not _shutdown_requested


def _handle_shutdown_signal(signum: int, _frame: FrameType | None) -> None:
    global _shutdown_requested
    log.info("worker.shutdown_requested", signal=signum)
    _shutdown_requested = True


def run_one_tick(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    *,
    settings: Settings,
    clock: Clock,
    whatsapp: WhatsAppProvider,
    sheets: SpreadsheetProvider | None,
    sheet_reader: Callable[[SheetLink], str] | None = None,
    worker_id: str,
    next_sheet_sync_at: dt.datetime,
) -> dt.datetime:
    """One full cycle: create today's reviews if their alert time has
    passed, then claim and dispatch due sends, then -- on its own, slower
    cadence -- reconcile against the Google Sheet. Each part gets its own
    session and its own commit, so a failure in one never blocks the
    others -- a broken share job must not stop tomorrow's review from being
    created, and a broken sheet sync must not stop either of those.

    Returns the next time the sheet-sync part should run, since it ticks on
    ``settings.sheet_sync_interval_seconds`` rather than every call here.
    """
    now = clock.now()

    session = session_factory()
    try:
        review_service = ReviewService(
            ApprovalRepository(session),
            PostgresTaskRepository(session),
            PostgresAuditSink(session),
            settings.tz,
        )
        created = maybe_create_daily_reviews(
            review_service,
            now=now,
            tz=settings.tz,
            working_days=settings.working_days,
            morning_alert_time=settings.morning_alert_time,
            evening_alert_time=settings.evening_alert_time,
        )
        session.commit()
        for result in created:
            if result.newly_created:
                log.info(
                    "worker.review_created",
                    kind=result.kind.value,
                    request_id=result.request.id,
                )
    except Exception:
        session.rollback()
        log.exception("worker.review_trigger_failed")
    finally:
        session.close()

    session = session_factory()
    try:
        scheduler = SchedulerService(
            scheduler_repo=SchedulerRepository(session),
            share_repo=ShareRepository(session),
            group_repo=WhatsAppGroupRepository(session),
            whatsapp=whatsapp,
            audit=PostgresAuditSink(session),
            tz=settings.tz,
            send_grace=settings.send_grace,
            retry_delays=settings.retry_delays_seconds,
            worker_id=worker_id,
            claim_lease=settings.claim_lease,
            claim_batch_size=settings.claim_batch_size,
            enforce_day_boundary=settings.enforce_day_boundary,
            strict_data_drift=settings.strict_data_drift,
        )
        tick_result = scheduler.tick(now=now)
        session.commit()
        if tick_result.actions_claimed or tick_result.retries_attempted or tick_result.leases_swept:
            log.info(
                "worker.tick",
                leases_swept=tick_result.leases_swept,
                actions_claimed=tick_result.actions_claimed,
                retries_attempted=tick_result.retries_attempted,
                sent=tick_result.sent,
                failed_sends=tick_result.failed_sends,
            )
    except Exception:
        session.rollback()
        log.exception("worker.scheduler_tick_failed")
    finally:
        session.close()

    # The read-only import and the two-way sync share this slot and its
    # cadence; the reader is None while the two-way sync owns the sheet. Which
    # sheet to read is the saved setting (changed from the browser), read afresh
    # every tick; a change asks for an import at once instead of waiting.
    if sheet_reader is not None and run_sheet_import(
        session_factory,
        settings=settings,
        now=now,
        reader=sheet_reader,
        interval_due=now >= next_sheet_sync_at,
    ):
        next_sheet_sync_at = now + dt.timedelta(seconds=settings.sheet_sync_interval_seconds)

    if sheets is not None and now >= next_sheet_sync_at:
        session = session_factory()
        try:
            sheet_sync = SheetSyncService(
                sheets=sheets,
                sync_repo=SyncRepository(session),
                task_repo=PostgresTaskRepository(session),
                audit=PostgresAuditSink(session),
            )
            sync_result = sheet_sync.tick(now=now)
            session.commit()
            if (
                sync_result.pushed
                or sync_result.pulled
                or sync_result.created
                or sync_result.conflicts
                or sync_result.vanished
            ):
                log.info(
                    "worker.sheet_sync_tick",
                    pushed=sync_result.pushed,
                    pulled=sync_result.pulled,
                    created=sync_result.created,
                    conflicts=sync_result.conflicts,
                    vanished=sync_result.vanished,
                )
        except Exception:
            session.rollback()
            log.exception("worker.sheet_sync_tick_failed")
        finally:
            session.close()
        next_sheet_sync_at = now + dt.timedelta(seconds=settings.sheet_sync_interval_seconds)

    return next_sheet_sync_at


def run_sheet_import(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    *,
    settings: Settings,
    now: dt.datetime,
    reader: Callable[[SheetLink], str],
    interval_due: bool,
) -> bool:
    """Import the saved sheet if it is due (the interval has passed, or its link
    just changed). Returns whether an attempt was made.

    Both outcomes are written down for the Settings page and the banner: a
    success with how many tasks the sheet held, a failure with a plain-language
    reason. The failure is recorded in its own transaction, after the import's
    has been rolled back, so a half-finished import can never be committed."""
    session = session_factory()
    try:
        repo = SheetSourceRepository(session)
        tasks = PostgresTaskRepository(session)
        audit = PostgresAuditSink(session)
        state = SheetSourceService(
            repo=repo,
            task_repo=tasks,
            audit=audit,
            tz=settings.tz,
            reader=reader,
            seed_url=settings.sheet_import_url,
        ).current(now=now)
        # Keep a first-time seed from .env whatever happens next: if the import
        # below fails and rolls back, the failure still needs a row to be written on.
        session.commit()
        if state is None or not state.configured or not (state.import_pending or interval_due):
            return False
        assert state.url is not None
        link = parse_sheet_link(state.url)
        result = SheetImportService(
            fetch=lambda: reader(link),
            task_repo=tasks,
            audit=audit,
            tz=settings.tz,
            scope=link.scope,
        ).tick(now=now)
        repo.record_success(now=now, task_count=result.rows_read)
        session.commit()
        if result.changed_anything or result.unrecognised_status:
            log.info(
                "worker.sheet_import_tick",
                created=result.created,
                updated=result.updated,
                hidden=result.hidden,
                unhidden=result.unhidden,
                retired=result.retired,
                unrecognised_status=list(result.unrecognised_status),
            )
        if result.skipped:
            log.warning(
                "worker.sheet_import_rows_skipped",
                rows=[
                    f"row {skip.line}: {skip.reason.value} {skip.detail}" for skip in result.skipped
                ],
            )
        return True
    except Exception as error:
        session.rollback()
        log.exception("worker.sheet_import_tick_failed")
        problem = classify_problem(error)
        try:
            SheetSourceRepository(session).record_failure(
                now=now, code=problem.code, message=problem.message
            )
            session.commit()
        except Exception:
            session.rollback()
            log.exception("worker.sheet_import_status_not_recorded")
        return True
    finally:
        session.close()


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )

    settings = get_settings()
    clock = SystemClock()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    whatsapp = build_whatsapp_provider(settings, clock=clock, session_factory=session_factory)
    # No provider at all under MOCK, rather than the in-memory fake: the fake
    # forgets every row when the process exits, but the reconciliation cursor
    # a tick writes to Postgres does not. Ticking against it would leave every
    # task "synced to a row that no longer exists", and the first real run
    # would park all of them as vanished instead of pushing them.
    sheets = (
        None
        if settings.sheets_provider is SheetsProviderName.MOCK
        else build_sheets_provider(settings)
    )
    sheet_reader = build_sheet_reader(settings)
    worker_id = f"worker-{socket.gethostname()}-{os.getpid()}"
    next_sheet_sync_at = clock.now()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    log.info(
        "worker.started",
        worker_id=worker_id,
        tick_seconds=settings.scheduler_tick_seconds,
        timezone=settings.timezone,
        sheets_provider=settings.sheets_provider.value,
        sheet_import=sheet_reader is not None,
        sheet_sync_interval_seconds=settings.sheet_sync_interval_seconds,
    )

    while _should_keep_running():
        try:
            next_sheet_sync_at = run_one_tick(
                session_factory,
                settings=settings,
                clock=clock,
                whatsapp=whatsapp,
                sheets=sheets,
                sheet_reader=sheet_reader,
                worker_id=worker_id,
                next_sheet_sync_at=next_sheet_sync_at,
            )
        except Exception:
            # A tick's own two halves already catch and log their own
            # failures; this is the last-resort guard against anything else
            # (a programming error in this loop itself) that could otherwise
            # kill the whole worker process.
            log.exception("worker.tick_crashed")

        for _ in range(settings.scheduler_tick_seconds):
            if not _should_keep_running():
                break
            time.sleep(1)

    log.info("worker.stopped", worker_id=worker_id)
    engine.dispose()


if __name__ == "__main__":
    main()
