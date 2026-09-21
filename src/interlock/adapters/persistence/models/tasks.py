"""Tasks and their history."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ, ULID_LENGTH, check_in
from interlock.domain.tasks.entities import Priority, TaskSourceKind, TaskStatus


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(check_in("status", TaskStatus), name="ck_tasks_status"),
        CheckConstraint(check_in("priority", Priority), name="ck_tasks_priority"),
        CheckConstraint(check_in("source", TaskSourceKind), name="ck_tasks_source"),
        CheckConstraint("version >= 1", name="ck_tasks_version_positive"),
        # "Overdue" is NOT a stored or generated column -- see the module note
        # in domain/tasks/derivations.py. PostgreSQL only allows immutable
        # expressions in a GENERATED column, and overdue-ness depends on wall
        # clock time, which moves with zero writes to the row. It is derived at
        # query/render time instead, which is what keeps it from ever going
        # stale. This partial index covers exactly the rows that could ever be
        # overdue -- open tasks carrying a due date -- so that a repository
        # query filtering on it stays fast without a redundant boolean to keep
        # in sync.
        Index(
            "idx_tasks_open_due_date",
            "due_date",
            postgresql_where="status <> 'COMPLETED' AND deleted_at IS NULL",
        ),
        Index("idx_tasks_owner", "owner_id"),
        Index("idx_tasks_project", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    display_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # No FK to a projects table: Phase 1 has no Project entity or behaviour
    # behind it (nothing in the brief's acceptance scenarios exercises one), so
    # this stays a plain label rather than schema for a concept with no logic
    # yet. Revisit if/when project management gets real behaviour.
    project_id: Mapped[str | None] = mapped_column(String(ULID_LENGTH), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(
        String(ULID_LENGTH), ForeignKey("users.id"), nullable=True
    )
    priority: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    due_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    remarks: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(50)), nullable=False, default=list)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TaskSourceKind.APP.value
    )
    external_row_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_shared_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class TaskHistoryRow(Base):
    """One row per version transition. Full point-in-time reconstruction.

    ``changed_by`` / ``changed_by_kind`` mirror
    :class:`interlock.domain.common.actor.Actor` rather than referencing a
    users row directly, because history must also record edits from a
    spreadsheet sync or the scheduler -- actors that are not users.
    """

    __tablename__ = "task_history"
    __table_args__ = (
        # One history row per version, never more -- guards against a bug that
        # tries to log the same transition twice.
        Index("uq_task_history_version", "task_id", "version", unique=True),
    )

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), ForeignKey("tasks.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False)
    changed_by: Mapped[str] = mapped_column(String(200), nullable=False)
    changed_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(ULID_LENGTH), nullable=True)
