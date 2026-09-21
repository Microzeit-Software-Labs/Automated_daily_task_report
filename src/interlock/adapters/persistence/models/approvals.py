"""Approvals, frozen snapshots, and the two delivery tables."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ, ULID_LENGTH, check_in
from interlock.domain.approvals.states import ApprovalState, RecipientState, ShareJobState
from interlock.domain.sharing.validity import DeferReason

APPROVAL_KINDS = ("MORNING", "EVENING", "MANUAL")


class ApprovalRequestRow(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(check_in("kind", APPROVAL_KINDS), name="ck_approval_kind"),
        CheckConstraint(check_in("state", ApprovalState), name="ck_approval_state"),
        # The duplicate-review guard -- but scoped to the two SCHEDULED kinds
        # only. A MANUAL review ("share the list right now") is legitimately
        # allowed more than once a day, so it is deliberately excluded from
        # this uniqueness rule rather than blocked by it.
        Index(
            "uq_approval_scheduled_once_per_day",
            "kind",
            "local_date",
            unique=True,
            postgresql_where="kind IN ('MORNING', 'EVENING')",
        ),
    )

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    display_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    local_date: Mapped[dt.date] = mapped_column(nullable=False)
    scheduled_for: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    opened_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    dataset_version_at_open: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(
        String(ULID_LENGTH), ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class ReportSnapshotRow(Base):
    """Immutable once written.

    UPDATE and DELETE are revoked from the application role in the migration
    that creates this table -- see its ``op.execute(...)`` calls. That is what
    makes "what you approved is what gets sent" a property the database
    enforces, not a convention application code has to honour.
    """

    __tablename__ = "report_snapshots"

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    approval_request_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), ForeignKey("approval_requests.id"), nullable=False
    )
    dataset_version: Mapped[str] = mapped_column(String(40), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rendered_body: Mapped[str] = mapped_column(Text, nullable=False)
    task_state: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    template_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class ShareJobRow(Base):
    __tablename__ = "share_jobs"
    __table_args__ = (
        CheckConstraint(check_in("state", ShareJobState), name="ck_share_job_state"),
        CheckConstraint(
            f"deferred_reason IS NULL OR {check_in('deferred_reason', DeferReason)}",
            name="ck_share_job_deferred_reason",
        ),
        # The double-click guard (Scenario 6): two commits against the same
        # review, each believing it is "the" share for that review, cannot both
        # produce a row. The loser's INSERT fails at the database -- not in
        # application code a future refactor could accidentally bypass.
        Index(
            "uq_share_job_action_version",
            "approval_request_id",
            "action_version",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    display_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    approval_request_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), ForeignKey("approval_requests.id"), nullable=False
    )
    # NOT NULL rather than a CHECK saying the same thing: every share_jobs row
    # is created only after the approval gate is passed (see
    # domain/approvals/machine.py's HUMAN_ONLY_TARGETS), so "has an approver and
    # a frozen snapshot" is true of every row that can ever exist. NOT NULL
    # states that directly.
    snapshot_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), ForeignKey("report_snapshots.id"), nullable=False
    )
    approved_by_user_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    scheduled_action_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH),
        ForeignKey("scheduled_actions.id"),
        unique=True,
        nullable=False,
    )
    action_version: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    deferred_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    deferred_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    sent_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class ShareRecipientRow(Base):
    """One group, one job. The actual unit of delivery."""

    __tablename__ = "share_recipients"
    __table_args__ = (
        CheckConstraint(check_in("state", RecipientState), name="ck_recipient_state"),
        # One delivery per group per job, ever (Scenario 8): retrying a job
        # must never create a second row for a group that already succeeded.
        Index(
            "uq_share_recipient_group",
            "share_job_id",
            "whatsapp_group_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    share_job_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), ForeignKey("share_jobs.id"), nullable=False
    )
    whatsapp_group_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), ForeignKey("whatsapp_groups.id"), nullable=False
    )
    client_message_id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), unique=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ack_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sent_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
