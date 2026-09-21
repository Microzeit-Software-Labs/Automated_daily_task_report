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
from types import FrameType

import structlog
from sqlalchemy.orm import sessionmaker

from interlock.adapters.persistence.approval_repository import ApprovalRepository
from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.base import make_engine, make_session_factory
from interlock.adapters.persistence.scheduler_repository import SchedulerRepository
from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.adapters.persistence.sync_repository import SyncRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.adapters.persistence.whatsapp_group_repository import WhatsAppGroupRepository
from interlock.adapters.sheets.fake import FakeSpreadsheetProvider
from interlock.adapters.sheets.google_sheets import GoogleSheetsProvider
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.config import Settings, SheetsProviderName, WhatsAppProviderName, get_settings
from interlock.domain.common.clock import SystemClock
from interlock.domain.ports.sheets import SpreadsheetProvider
from interlock.domain.ports.whatsapp import WhatsAppProvider
from interlock.services.review_service import ReviewService
from interlock.services.review_trigger import maybe_create_daily_reviews
from interlock.services.scheduler_service import SchedulerService
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


def _build_whatsapp_provider(settings: Settings, clock: SystemClock) -> WhatsAppProvider:
    if settings.whatsapp_provider is not WhatsAppProviderName.MOCK:
        raise NotImplementedError(
            f"WHATSAPP_PROVIDER={settings.whatsapp_provider.value!r} is not implemented "
            "in Phase 1. Only 'mock' is available until the local agent (Phase 3) ships."
        )
    return MockWhatsAppProvider(clock)


def _build_sheets_provider(settings: Settings) -> SpreadsheetProvider:
    if settings.sheets_provider is SheetsProviderName.MOCK:
        return FakeSpreadsheetProvider()
    if not settings.google_service_account_path or not settings.google_sheets_spreadsheet_id:
        raise RuntimeError(
            "SHEETS_PROVIDER=google requires GOOGLE_SERVICE_ACCOUNT_PATH and "
            "GOOGLE_SHEETS_SPREADSHEET_ID to be set -- see docs/google-sheets-setup.md."
        )
    return GoogleSheetsProvider(
        service_account_path=settings.google_service_account_path,
        spreadsheet_id=settings.google_sheets_spreadsheet_id,
        sheet_name=settings.google_sheets_sheet_name,
    )


def run_one_tick(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    *,
    settings: Settings,
    clock: SystemClock,
    whatsapp: WhatsAppProvider,
    sheets: SpreadsheetProvider,
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

    if now >= next_sheet_sync_at:
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
    whatsapp = _build_whatsapp_provider(settings, clock)
    sheets = _build_sheets_provider(settings)
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
