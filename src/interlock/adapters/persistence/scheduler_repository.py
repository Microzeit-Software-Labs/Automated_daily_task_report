"""The durable scheduler's storage: create, claim, and sweep.

``claim_due`` is where the Phase 0 architecture's central deviation from
Celery ETA tasks actually lives: due rows are claimed with
``FOR UPDATE SKIP LOCKED`` rather than held in a worker's in-memory prefetch
buffer, so a claim survives -- and a crashed worker's claim is recoverable by
-- a process restart. Multiple worker processes can run this same query
concurrently and never claim the same row twice.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import ScheduledActionRow
from interlock.domain.common.errors import NotFoundError
from interlock.domain.common.ids import new_id
from interlock.domain.scheduling.action import ActionStatus, ScheduledAction


class SchedulerRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, *, kind: str, run_at: dt.datetime, payload: dict[str, Any], now: dt.datetime
    ) -> ScheduledAction:
        row = ScheduledActionRow(
            id=new_id(),
            kind=kind,
            run_at=run_at,
            payload=payload,
            status=ActionStatus.PENDING.value,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _to_entity(row)

    def get(self, action_id: str) -> ScheduledAction | None:
        row = self._session.get(ScheduledActionRow, action_id)
        return None if row is None else _to_entity(row)

    def claim_due(
        self, *, now: dt.datetime, worker_id: str, lease: dt.timedelta, batch_size: int
    ) -> list[ScheduledAction]:
        """Claim up to ``batch_size`` rows that are due, atomically.

        ``FOR UPDATE SKIP LOCKED`` is what makes this safe with multiple
        worker processes: a row already locked by another claimant is simply
        skipped rather than waited on, so claims never block each other and
        never double-claim the same row.
        """
        due_ids = self._session.execute(
            select(ScheduledActionRow.id)
            .where(
                ScheduledActionRow.status == ActionStatus.PENDING.value,
                ScheduledActionRow.run_at <= now,
            )
            .order_by(ScheduledActionRow.run_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).scalars().all()

        if not due_ids:
            return []

        self._session.execute(
            update(ScheduledActionRow)
            .where(ScheduledActionRow.id.in_(due_ids))
            .values(
                status=ActionStatus.CLAIMED.value,
                claimed_at=now,
                claimed_by=worker_id,
                lease_until=now + lease,
                updated_at=now,
            )
        )
        self._session.flush()

        rows = self._session.execute(
            select(ScheduledActionRow).where(ScheduledActionRow.id.in_(due_ids))
        ).scalars().all()
        return [_to_entity(row) for row in rows]

    def mark_done(self, action_id: str, *, now: dt.datetime) -> None:
        self._set_status(action_id, ActionStatus.DONE, now=now)

    def mark_failed(self, action_id: str, *, error: str, now: dt.datetime) -> None:
        row = self._session.get(ScheduledActionRow, action_id)
        if row is None:
            raise NotFoundError(
                f"Scheduled action {action_id} does not exist.", action_id=action_id
            )
        row.status = ActionStatus.FAILED.value
        row.last_error = error
        row.attempts += 1
        row.updated_at = now
        self._session.flush()

    def mark_cancelled(self, action_id: str, *, now: dt.datetime) -> None:
        self._set_status(action_id, ActionStatus.CANCELLED, now=now)

    def reschedule(self, action_id: str, *, run_at: dt.datetime, now: dt.datetime) -> None:
        row = self._session.get(ScheduledActionRow, action_id)
        if row is None:
            raise NotFoundError(
                f"Scheduled action {action_id} does not exist.", action_id=action_id
            )
        row.run_at = run_at
        row.status = ActionStatus.PENDING.value
        row.claimed_at = None
        row.claimed_by = None
        row.lease_until = None
        row.updated_at = now
        self._session.flush()

    def sweep_expired_leases(self, *, now: dt.datetime) -> int:
        """Return CLAIMED rows whose lease expired -- a worker crashed or was
        killed mid-send -- back to PENDING so another worker can pick them up.
        This is what makes a worker restart safe: nothing is lost, it is
        simply reclaimed on the next tick after the lease window passes."""
        # Cast to CursorResult: see the identical note in task_repository.py's
        # update() -- an UPDATE has a real .rowcount at runtime that the
        # generic Result[Any] stub does not declare.
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(ScheduledActionRow)
                .where(
                    ScheduledActionRow.status == ActionStatus.CLAIMED.value,
                    ScheduledActionRow.lease_until < now,
                )
                .values(
                    status=ActionStatus.PENDING.value,
                    claimed_at=None,
                    claimed_by=None,
                    lease_until=None,
                    updated_at=now,
                )
            ),
        )
        self._session.flush()
        return result.rowcount

    def _set_status(self, action_id: str, status: ActionStatus, *, now: dt.datetime) -> None:
        row = self._session.get(ScheduledActionRow, action_id)
        if row is None:
            raise NotFoundError(
                f"Scheduled action {action_id} does not exist.", action_id=action_id
            )
        row.status = status.value
        row.updated_at = now
        self._session.flush()


def _to_entity(row: ScheduledActionRow) -> ScheduledAction:
    return ScheduledAction(
        id=row.id,
        kind=row.kind,
        run_at=row.run_at,
        payload=dict(row.payload),
        status=ActionStatus(row.status),
        attempts=row.attempts,
        created_at=row.created_at,
        updated_at=row.updated_at,
        claimed_at=row.claimed_at,
        claimed_by=row.claimed_by,
        lease_until=row.lease_until,
        last_error=row.last_error,
    )
