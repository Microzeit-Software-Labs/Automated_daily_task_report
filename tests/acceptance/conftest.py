"""Acceptance test harness: the real HTTP API, a real database, no network.

Unlike tests/integration/ (one rolled-back transaction per test), these tests
drive the API exactly as a real client would -- each HTTP call opens and
commits its own session, same as production. That means isolation between
tests comes from truncating the schema before each test, not from a
rollback, and it is also what makes Scenario 5's "the server restarts"
meaningful to actually test: a fresh ``create_app()`` with a fresh engine
reading the same durable rows proves persistence rather than assuming it.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from interlock.adapters.persistence.approval_repository import ApprovalRepository
from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.base import make_engine
from interlock.adapters.persistence.scheduler_repository import SchedulerRepository
from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.adapters.persistence.whatsapp_group_repository import WhatsAppGroupRepository
from interlock.adapters.sheets.fake import FakeSpreadsheetProvider
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.api.main import create_app
from interlock.config import ReportFormat, Settings
from interlock.domain.common.clock import FrozenClock
from interlock.services.review_service import ReviewService
from interlock.services.review_trigger import TriggerResult, maybe_create_daily_reviews
from interlock.services.scheduler_service import SchedulerService, TickResult

load_dotenv()

pytestmark = pytest.mark.acceptance

T0 = dt.datetime(2026, 9, 15, 17, 0, tzinfo=dt.UTC)  # 22:30 IST
MORNING = dt.datetime(2026, 9, 15, 3, 30, tzinfo=dt.UTC)  # 09:00 IST
EVENING = dt.datetime(2026, 9, 15, 11, 30, tzinfo=dt.UTC)  # 17:00 IST


def _test_settings() -> Settings:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set. Run scripts/setup-database.ps1 first.")
    # The nine scenarios pin the text format (they assert on message text);
    # the image format has its own tests in test_image_reports.py.
    return Settings(database_url=url, report_format=ReportFormat.TEXT)


@pytest.fixture(scope="session")
def _migrated() -> None:
    """Migrate the test database to head exactly once for the whole run."""
    from alembic import command
    from alembic.config import Config

    settings = _test_settings()
    os.environ["MIGRATION_DATABASE_URL"] = settings.database_url
    repo_root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(repo_root / "alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


@pytest.fixture
def clean_db(_migrated: None) -> Iterator[None]:
    """Every acceptance test starts from a genuinely empty schema. These
    tests cannot rely on a rolled-back transaction for isolation -- each HTTP
    call commits its own session, same as production -- so isolation comes
    from truncating before each test instead."""
    settings = _test_settings()
    engine = make_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE "
                "share_recipients, share_jobs, report_snapshots, task_history, "
                "tasks, approval_requests, scheduled_actions, whatsapp_groups, "
                "idempotency_keys, audit_logs, users, "
                "whatsapp_agent_commands, whatsapp_agent_status "
                "RESTART IDENTITY CASCADE"
            )
        )
    engine.dispose()
    yield


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def whatsapp(clock: FrozenClock) -> MockWhatsAppProvider:
    return MockWhatsAppProvider(clock)


@pytest.fixture
def sheets() -> FakeSpreadsheetProvider:
    return FakeSpreadsheetProvider()


@pytest.fixture
def client(
    clean_db: None,
    clock: FrozenClock,
    whatsapp: MockWhatsAppProvider,
    sheets: FakeSpreadsheetProvider,
) -> Iterator[TestClient]:
    settings = _test_settings()
    app = create_app(
        settings=settings, clock=clock, whatsapp_provider=whatsapp, sheets_provider=sheets
    )
    with TestClient(app) as test_client:
        yield test_client


def trigger_reviews(*, now: dt.datetime) -> list[TriggerResult]:
    """The other half of the worker loop: creates today's morning and/or
    evening review if their alert time has passed, standing in for the
    background worker the way ``run_tick`` stands in for it on the send side.

    Real HTTP endpoints only ever *use* a review that already exists
    (``GET/POST /approval-requests/{id}/...``) -- nothing calls this over
    HTTP, in the acceptance tests or in production, because in production it
    is the worker loop that notices 09:00/17:00 arrived, never a browser.
    """
    settings = _test_settings()
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            session = sessionmaker(bind=conn, future=True)()
            review_service = ReviewService(
                ApprovalRepository(session),
                PostgresTaskRepository(session),
                PostgresAuditSink(session),
                settings.tz,
            )
            results = maybe_create_daily_reviews(
                review_service,
                now=now,
                tz=settings.tz,
                working_days=settings.working_days,
                morning_alert_time=settings.morning_alert_time,
                evening_alert_time=settings.evening_alert_time,
            )
            session.commit()
            return results
    finally:
        engine.dispose()


def run_tick(whatsapp: MockWhatsAppProvider, *, now: dt.datetime) -> TickResult:
    """Run one scheduler tick against a fresh engine and session -- standing
    in for the separate worker process production always uses, including
    across "the server restarted" (Scenario 5): the durable state this reads
    lives entirely in Postgres, never in this function's caller.

    Takes ``whatsapp`` as a parameter rather than constructing its own, so it
    is the *same* provider instance a test's ``client`` fixture wired into
    the API -- otherwise a test could commit a share through the API and then
    have no way to observe what the scheduler actually sent.
    """
    settings = _test_settings()
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            session = sessionmaker(bind=conn, future=True)()
            service = SchedulerService(
                scheduler_repo=SchedulerRepository(session),
                share_repo=ShareRepository(session),
                group_repo=WhatsAppGroupRepository(session),
                whatsapp=whatsapp,
                audit=PostgresAuditSink(session),
                tz=settings.tz,
                send_grace=settings.send_grace,
                retry_delays=settings.retry_delays_seconds,
                worker_id="acceptance-worker",
                claim_lease=settings.claim_lease,
                claim_batch_size=settings.claim_batch_size,
            )
            result = service.tick(now=now)
            session.commit()
            return result
    finally:
        engine.dispose()
