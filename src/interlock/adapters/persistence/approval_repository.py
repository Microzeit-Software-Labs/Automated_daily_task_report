"""PostgreSQL-backed approval-request storage.

Not behind a swappable port, unlike TaskRepository: the architecture treats
Postgres as the permanent home for reviews, share jobs and scheduling (see
Phase 0's "Postgres is the schedule of record" deviation) -- there is no plan
to ever back this with Google Sheets or anything else, so a Protocol
abstraction here would be ceremony with no second implementation behind it.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import (
    ApprovalRequestRow,
    ReportSnapshotRow,
    ShareJobRow,
)
from interlock.adapters.persistence.sequences import PostgresSequences
from interlock.domain.approvals.request import ApprovalKind, ApprovalRequest
from interlock.domain.approvals.states import ApprovalState
from interlock.domain.common.errors import NotFoundError
from interlock.domain.common.ids import REVIEW_PREFIX, format_display_id, new_id


class ApprovalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, request_id: str) -> ApprovalRequest | None:
        row = self._session.get(ApprovalRequestRow, request_id)
        return None if row is None else _to_entity(row)

    def get_by_display_id(self, display_id: str) -> ApprovalRequest | None:
        row = self._session.execute(
            select(ApprovalRequestRow).where(ApprovalRequestRow.display_id == display_id)
        ).scalar_one_or_none()
        return None if row is None else _to_entity(row)

    def get_scheduled_for_day(
        self, kind: ApprovalKind, local_date: dt.date
    ) -> ApprovalRequest | None:
        row = self._session.execute(
            select(ApprovalRequestRow).where(
                ApprovalRequestRow.kind == kind.value,
                ApprovalRequestRow.local_date == local_date,
            )
        ).scalar_one_or_none()
        return None if row is None else _to_entity(row)

    def list_recent(
        self, *, limit: int = 20, local_date: dt.date | None = None
    ) -> list[ApprovalRequest]:
        """Newest first by ``scheduled_for`` -- what the dashboard lists. With
        ``local_date``, only that day's reviews."""
        query = select(ApprovalRequestRow)
        if local_date is not None:
            query = query.where(ApprovalRequestRow.local_date == local_date)
        rows = self._session.execute(
            query.order_by(
                ApprovalRequestRow.scheduled_for.desc(), ApprovalRequestRow.display_id.desc()
            ).limit(limit)
        ).scalars()
        return [_to_entity(row) for row in rows]

    def create_scheduled(
        self,
        *,
        kind: ApprovalKind,
        local_date: dt.date,
        scheduled_for: dt.datetime,
        now: dt.datetime,
    ) -> tuple[ApprovalRequest, bool]:
        """Create the review for ``(kind, local_date)``, or return the one
        that already exists.

        This is the mechanism that makes the 09:00/17:00 tick idempotent: if
        the scheduler fires twice for the same morning -- a restart, a retried
        tick -- the second attempt hits the partial unique index on
        ``(kind, local_date)`` for MORNING/EVENING and this method returns the
        *existing* row instead of raising. Returns ``(request, created)`` so
        the caller can tell which happened, e.g. to decide whether to push a
        notification.

        A MANUAL review never collides here: it has no uniqueness constraint
        to hit, so it always creates a fresh row.
        """
        candidate = ApprovalRequestRow(
            id=new_id(),
            display_id=format_display_id(
                REVIEW_PREFIX, PostgresSequences(self._session).next("review")
            ),
            kind=kind.value,
            local_date=local_date,
            scheduled_for=scheduled_for,
            state=ApprovalState.REVIEW_PENDING.value,
            created_at=now,
            updated_at=now,
        )

        if not kind.is_scheduled:
            self._session.add(candidate)
            self._session.flush()
            return _to_entity(candidate), True

        savepoint = self._session.begin_nested()
        try:
            self._session.add(candidate)
            self._session.flush()
            savepoint.commit()
            return _to_entity(candidate), True
        except IntegrityError:
            savepoint.rollback()
            existing = self.get_scheduled_for_day(kind, local_date)
            if existing is None:
                # Practically unreachable: the insert failed on the very
                # constraint that would make this row findable. Re-raising
                # is safer than silently returning nothing.
                raise
            return existing, False

    def save_transition(
        self,
        request: ApprovalRequest,
        *,
        target_state: ApprovalState,
        now: dt.datetime,
        opened_at: dt.datetime | None = None,
        dataset_version_at_open: str | None = None,
        approved_by_user_id: str | None = None,
        approved_at: dt.datetime | None = None,
    ) -> ApprovalRequest:
        row = self._session.get(ApprovalRequestRow, request.id)
        if row is None:
            raise NotFoundError(
                f"Approval request {request.id} does not exist.", request_id=request.id
            )
        row.state = target_state.value
        row.updated_at = now
        if opened_at is not None:
            row.opened_at = opened_at
        if dataset_version_at_open is not None:
            row.dataset_version_at_open = dataset_version_at_open
        if approved_by_user_id is not None:
            row.approved_by_user_id = approved_by_user_id
        if approved_at is not None:
            row.approved_at = approved_at
        self._session.flush()
        return _to_entity(row)

    def last_successful_share_at(self, before: dt.datetime) -> dt.datetime | None:
        """When the most recent SENT share job's snapshot was frozen, if any.

        Feeds "changes since last share" -- the change summary shown before a
        new report is approved.
        """
        return self._session.execute(
            select(ReportSnapshotRow.created_at)
            .join(ShareJobRow, ShareJobRow.snapshot_id == ReportSnapshotRow.id)
            .where(ShareJobRow.state == "SENT", ReportSnapshotRow.created_at < before)
            .order_by(ReportSnapshotRow.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()


def _to_entity(row: ApprovalRequestRow) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        display_id=row.display_id,
        kind=ApprovalKind(row.kind),
        local_date=row.local_date,
        scheduled_for=row.scheduled_for,
        state=ApprovalState(row.state),
        created_at=row.created_at,
        updated_at=row.updated_at,
        opened_at=row.opened_at,
        dataset_version_at_open=row.dataset_version_at_open,
        approved_by_user_id=row.approved_by_user_id,
        approved_at=row.approved_at,
    )
