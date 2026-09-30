"""Review (ApprovalRequest) lifecycle orchestration.

State transitions are never assigned directly here -- every one goes through
:func:`interlock.domain.approvals.machine.transition`, which validates
legality and, for ``APPROVED``, enforces that only a human actor can reach it.
This module records the resulting audit entry and then asks the repository to
persist the new state; it never sets ``row.state`` itself.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from interlock.adapters.persistence.approval_repository import ApprovalRepository
from interlock.domain.approvals.machine import transition
from interlock.domain.approvals.request import ApprovalKind, ApprovalRequest
from interlock.domain.approvals.states import ApprovalState
from interlock.domain.common.actor import Actor, system_actor
from interlock.domain.common.clock import local_date
from interlock.domain.common.errors import NotFoundError
from interlock.domain.ports.repositories import AuditSink, TaskRepository
from interlock.domain.sharing.snapshot import format_dataset_version
from interlock.domain.tasks.entities import Task
from interlock.domain.tasks.summary import (
    ChangeSummary,
    TaskSummary,
    summarize,
    summarize_changes,
)

SCHEDULER_ACTOR = system_actor("scheduler")


@dataclasses.dataclass(frozen=True, slots=True)
class ReviewDetail:
    request: ApprovalRequest
    tasks: Sequence[Task]
    summary: TaskSummary
    changes_since_last_share: ChangeSummary


class ReviewService:
    def __init__(
        self,
        approval_repo: ApprovalRepository,
        task_repo: TaskRepository,
        audit: AuditSink,
        tz: ZoneInfo,
    ) -> None:
        self._approvals = approval_repo
        self._tasks = task_repo
        self._audit = audit
        self._tz = tz

    def create_scheduled_review(
        self, kind: ApprovalKind, *, scheduled_for: dt.datetime, now: dt.datetime
    ) -> tuple[ApprovalRequest, bool]:
        """The 09:00 / 17:00 tick's entry point.

        Idempotent by construction: if this kind's review for today already
        exists -- a restart, a retried tick -- the existing row is returned
        rather than a duplicate being created. Returns ``(request, created)``
        so the caller can decide whether a fresh notification is warranted.
        """
        day = local_date(scheduled_for, self._tz)
        request, created = self._approvals.create_scheduled(
            kind=kind, local_date=day, scheduled_for=scheduled_for, now=now
        )
        if created:
            self._audit.record(
                action="REVIEW_CREATED",
                entity_type="approval_request",
                entity_id=request.id,
                actor=str(SCHEDULER_ACTOR),
                actor_kind=SCHEDULER_ACTOR.kind.value,
                at=now,
                after={"kind": kind.value, "local_date": day.isoformat()},
            )
        return request, created

    def create_manual(self, *, actor: Actor, now: dt.datetime) -> ApprovalRequest:
        """"Share the list right now", outside 09:00 / 17:00. A MANUAL review
        has no once-per-day guard, so this always creates a fresh row. It
        still goes through the same approval gate as a scheduled one --
        creating a review sends nothing."""
        day = local_date(now, self._tz)
        request, _created = self._approvals.create_scheduled(
            kind=ApprovalKind.MANUAL, local_date=day, scheduled_for=now, now=now
        )
        self._audit.record(
            action="REVIEW_CREATED",
            entity_type="approval_request",
            entity_id=request.id,
            actor=str(actor),
            actor_kind=actor.kind.value,
            at=now,
            after={"kind": ApprovalKind.MANUAL.value, "local_date": day.isoformat()},
        )
        return request

    def list_recent(
        self, *, limit: int = 20, day: dt.date | None = None
    ) -> list[ApprovalRequest]:
        return self._approvals.list_recent(limit=limit, local_date=day)

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._approvals.get(request_id)

    def get_detail(self, request_id: str) -> ReviewDetail | None:
        """Everything the review panel needs in one call: the request, the
        live task list, the summary counts, and what changed since the last
        successful share."""
        request = self._approvals.get(request_id)
        if request is None:
            return None

        tasks = self._tasks.list()
        today = local_date(request.scheduled_for, self._tz)
        summary = summarize(tasks, today=today, tz=self._tz)
        since = self._approvals.last_successful_share_at(before=request.scheduled_for)
        changes = summarize_changes(tasks, since=since, today=today)
        return ReviewDetail(
            request=request,
            tasks=tasks,
            summary=summary,
            changes_since_last_share=changes,
        )

    def open(self, request_id: str, *, actor: Actor, now: dt.datetime) -> ApprovalRequest:
        """REVIEW_PENDING -> USER_EDITING. Records when, and against what
        data label, the user started looking at this review."""
        request = self._require(request_id)

        if request.state is ApprovalState.USER_EDITING:
            return request  # already open; opening again is a no-op

        record = transition(
            request.state,
            ApprovalState.USER_EDITING,
            entity_type="approval_request",
            entity_id=request.id,
            actor=actor,
            at=now,
        )
        self._audit.record(
            action="REVIEW_OPENED",
            entity_type="approval_request",
            entity_id=request.id,
            actor=record.actor,
            actor_kind=record.actor_kind,
            at=now,
            before={"state": record.from_state},
            after={"state": record.to_state},
        )
        return self._approvals.save_transition(
            request,
            target_state=ApprovalState.USER_EDITING,
            now=now,
            opened_at=now,
            dataset_version_at_open=format_dataset_version(now, self._tz, sequence=0),
        )

    def cancel(self, request_id: str, *, actor: Actor, now: dt.datetime) -> ApprovalRequest:
        request = self._require(request_id)
        record = transition(
            request.state,
            ApprovalState.CANCELLED,
            entity_type="approval_request",
            entity_id=request.id,
            actor=actor,
            at=now,
        )
        self._audit.record(
            action="REVIEW_CANCELLED",
            entity_type="approval_request",
            entity_id=request.id,
            actor=record.actor,
            actor_kind=record.actor_kind,
            at=now,
            before={"state": record.from_state},
            after={"state": record.to_state},
        )
        return self._approvals.save_transition(
            request, target_state=ApprovalState.CANCELLED, now=now
        )

    def _require(self, request_id: str) -> ApprovalRequest:
        request = self._approvals.get(request_id)
        if request is None:
            raise NotFoundError(
                f"Approval request {request_id} does not exist.", request_id=request_id
            )
        return request
