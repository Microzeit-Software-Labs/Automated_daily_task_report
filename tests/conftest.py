"""Shared test fixtures.

Builders rather than fixtures for entities: a test that says
``make_task(status=COMPLETED, due_date=yesterday)`` reads as the scenario it is
testing, which a shared fixture object does not.
"""

from __future__ import annotations

import datetime as dt
import itertools
from zoneinfo import ZoneInfo

import pytest
from dotenv import load_dotenv

from interlock.domain.common.ids import format_display_id, new_id
from interlock.domain.tasks.entities import Priority, Task, TaskStatus

# Loaded once, here, so integration tests see TEST_DATABASE_URL /
# MIGRATION_DATABASE_URL / DATABASE_URL from .env without every test module
# needing to know that file exists. Unit tests need none of this -- they never
# read the environment -- so it is harmless for them to have it loaded too.
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

# 15 September 2026, 17:04 IST -- the moment used throughout the product brief.
BRIEF_EVENING = dt.datetime(2026, 9, 15, 17, 4, tzinfo=IST).astimezone(dt.UTC)
BRIEF_MORNING = dt.datetime(2026, 9, 15, 9, 0, tzinfo=IST).astimezone(dt.UTC)

_sequence = itertools.count(1)


def make_task(
    *,
    title: str = "Deploy production server",
    status: TaskStatus = TaskStatus.PENDING,
    priority: Priority = Priority.MEDIUM,
    due_date: dt.date | None = None,
    created_at: dt.datetime | None = None,
    updated_at: dt.datetime | None = None,
    completed_at: dt.datetime | None = None,
    version: int = 1,
    **extra: object,
) -> Task:
    created = created_at or BRIEF_EVENING - dt.timedelta(days=3)
    return Task(
        id=new_id(),
        display_id=format_display_id("TSK", next(_sequence)),
        title=title,
        status=status,
        priority=priority,
        created_at=created,
        updated_at=updated_at or created,
        completed_at=completed_at,
        due_date=due_date,
        version=version,
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def ist() -> ZoneInfo:
    return IST


@pytest.fixture
def evening() -> dt.datetime:
    return BRIEF_EVENING
