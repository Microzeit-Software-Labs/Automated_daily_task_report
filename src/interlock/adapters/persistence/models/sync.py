"""Google Sheets reconciliation bookkeeping.

Two tables, one per concern: ``task_sheet_sync`` is the cursor a
reconciliation tick reads to know what "last agreed" means for a task;
``sync_conflicts`` is where a tick parks a row it refused to touch because
both sides had moved. Neither is ever written by the application's normal
task-mutation path -- both are owned exclusively by
``services/sheet_sync_service.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ, ULID_LENGTH, check_in

RESOLUTIONS = ("kept_db", "kept_sheet")


class TaskSheetSyncRow(Base):
    """The reconciliation cursor. One row per task, once first synced in
    either direction."""

    __tablename__ = "task_sheet_sync"

    task_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), ForeignKey("tasks.id"), primary_key=True
    )
    last_synced_version: Mapped[int] = mapped_column(nullable=False)
    last_synced_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_synced_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    last_known_row_index: Mapped[int | None] = mapped_column(nullable=True)
    """Diagnostic only. Never trusted for row identity -- a tick always
    relocates a task's row by scanning the freshly read ``_sys`` column."""


class SyncConflictRow(Base):
    """A parked collision. Deliberately never auto-resolved -- see the
    architecture doc's R5 on spreadsheet edits outside the app."""

    __tablename__ = "sync_conflicts"
    __table_args__ = (
        CheckConstraint(check_in("resolution", RESOLUTIONS), name="ck_sync_conflicts_resolution"),
        # At most one OPEN conflict per task: a tick that keeps finding the
        # same unresolved collision every cycle must not pile up duplicates.
        Index(
            "uq_sync_conflicts_open",
            "task_id",
            unique=True,
            postgresql_where="resolution IS NULL",
        ),
    )

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), ForeignKey("tasks.id"), nullable=False
    )
    detected_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    db_value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    external_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """``None`` means the row vanished from the sheet rather than being edited."""
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
