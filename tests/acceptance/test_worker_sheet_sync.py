"""The worker loop's sheet-sync branch.

``run_one_tick`` had no test at all: nothing proved that the sync branch runs
when due, commits, and reschedules itself -- or that under
``SHEETS_PROVIDER=mock`` (no provider) it does nothing, which matters because
the reconciliation cursor lands in Postgres and outlives the in-memory fake.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from starlette.testclient import TestClient

from interlock.adapters.persistence.base import make_engine, make_session_factory
from interlock.adapters.sheets.fake import FakeSpreadsheetProvider
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from interlock.domain.ports.sheets import SpreadsheetProvider
from interlock.workers.loop import run_one_tick
from tests.acceptance.conftest import T0, _test_settings


def tick(
    sheets: SpreadsheetProvider | None, *, next_sync_at: dt.datetime
) -> dt.datetime:
    settings = _test_settings()
    clock = FrozenClock(T0)
    engine = make_engine(settings.database_url)
    try:
        return run_one_tick(
            make_session_factory(engine),
            settings=settings,
            clock=clock,
            whatsapp=MockWhatsAppProvider(clock),
            sheets=sheets,
            worker_id="test-worker",
            next_sheet_sync_at=next_sync_at,
        )
    finally:
        engine.dispose()


def cursor_count() -> int:
    engine = make_engine(_test_settings().database_url)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text("SELECT count(*) FROM task_sheet_sync")).scalar_one())
    finally:
        engine.dispose()


def create_task(client: TestClient) -> None:
    assert client.post("/tasks", json={"title": "Deploy production server"}).status_code == 201


def test_syncs_when_due_and_schedules_the_next_run(
    client: TestClient, sheets: FakeSpreadsheetProvider
) -> None:
    create_task(client)

    next_at = tick(sheets, next_sync_at=T0)

    assert len(sheets.read_all_rows()) == 1
    assert cursor_count() == 1
    interval = dt.timedelta(seconds=_test_settings().sheet_sync_interval_seconds)
    assert next_at == T0 + interval


def test_does_not_sync_before_it_is_due(
    client: TestClient, sheets: FakeSpreadsheetProvider
) -> None:
    create_task(client)
    not_yet = T0 + dt.timedelta(hours=1)

    next_at = tick(sheets, next_sync_at=not_yet)

    assert sheets.read_all_rows() == []
    assert cursor_count() == 0
    assert next_at == not_yet


def test_no_provider_means_no_sync_and_no_cursor_is_written(client: TestClient) -> None:
    """SHEETS_PROVIDER=mock passes no provider. Writing a cursor against a
    sheet that forgets everything on exit would make the first real run park
    every task as vanished."""
    create_task(client)

    tick(None, next_sync_at=T0)

    assert cursor_count() == 0
