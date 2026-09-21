"""The durable action queue.

This is what makes Postgres the schedule of record (see the Phase 0 deviation
from Celery ETA tasks): a row here survives a process restart because it lives
in the database, and the worker claims due rows with ``FOR UPDATE SKIP LOCKED``
rather than holding them in a prefetch buffer.

Deliberately generic and decoupled from any one domain aggregate's own state
machine. This table's ``status`` answers "has a worker claimed and finished
this row" -- never "what is the business outcome", which lives on whatever the
payload points at (e.g. a ShareJob's own ``state``). Conflating the two would
make "retry only the failed recipient" unrepresentable at this layer.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import CheckConstraint, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ, ULID_LENGTH, check_in

STATUSES = ("PENDING", "CLAIMED", "DONE", "FAILED", "CANCELLED")


class ScheduledActionRow(Base):
    __tablename__ = "scheduled_actions"
    __table_args__ = (
        CheckConstraint(check_in("status", STATUSES), name="ck_scheduled_actions_status"),
        # Only PENDING rows are ever claimed, so only they need to be fast to
        # find. A full index over every historical row (DONE, FAILED, ...)
        # would grow forever for no benefit the scheduler ever uses.
        Index(
            "idx_scheduled_actions_due",
            "run_at",
            postgresql_where="status = 'PENDING'",
        ),
    )

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    run_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    claimed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_until: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
