"""The ``/commit`` orchestration: UPDATE_ONLY, UPDATE_AND_SHARE, SHARE_ONLY.

All three collapse into one method rather than three endpoints for the reason
given in the Phase 0 architecture: kept separate, a client could call update
then share and produce a report whose snapshot sits between two edits --
exactly the inconsistency snapshotting exists to prevent. One method, one
transaction, one ``mode`` discriminator.

Idempotency is layered twice, deliberately:

1. An early check, before any work: if a share job already exists for
   ``(approval_request_id, action_version)``, return it immediately. This is
   what makes a sequential retry -- the overwhelmingly common case for a real
   double-click -- cheap and side-effect-free.
2. The repository's own INSERT, which resolves a genuine race between
   concurrent callers (see ShareRepository.create_job). A caller that loses
   that race cancels the scheduled_action row it just created rather than
   leaving it orphaned for the scheduler to find with no job attached.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from enum import StrEnum
from zoneinfo import ZoneInfo

from interlock.adapters.persistence.approval_repository import ApprovalRepository
from interlock.adapters.persistence.scheduler_repository import SchedulerRepository
from interlock.adapters.persistence.sequences import PostgresSequences
from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.domain.approvals.machine import transition
from interlock.domain.approvals.request import ApprovalRequest
from interlock.domain.approvals.states import ApprovalState, ShareJobState
from interlock.domain.common.actor import Actor
from interlock.domain.common.clock import local_date
from interlock.domain.common.errors import (
    IllegalTransitionError,
    NoRecipientsSelectedError,
    NotFoundError,
)
from interlock.domain.ports.repositories import AuditSink, TaskRepository
from interlock.domain.scheduling.action import SEND_SHARE_JOB
from interlock.domain.sharing.job import ShareJob, ShareRecipient
from interlock.domain.sharing.render import (
    REVIEW_TITLES,
    build_context,
    default_template_for,
    render_report,
)
from interlock.domain.sharing.snapshot import freeze
from interlock.domain.tasks.summary import summarize
from interlock.services.task_service import BulkUpdateItem, TaskService

_COMMITTABLE_STATES = frozenset(
    {
        ApprovalState.REVIEW_PENDING,
        ApprovalState.USER_EDITING,
        ApprovalState.READY,
        ApprovalState.APPROVED,  # allowed through for the idempotent-retry path only
    }
)


class CommitMode(StrEnum):
    UPDATE_ONLY = "UPDATE_ONLY"
    UPDATE_AND_SHARE = "UPDATE_AND_SHARE"
    SHARE_ONLY = "SHARE_ONLY"

    @property
    def edits_tasks(self) -> bool:
        return self is not CommitMode.SHARE_ONLY

    @property
    def produces_a_share(self) -> bool:
        return self is not CommitMode.UPDATE_ONLY


@dataclasses.dataclass(frozen=True, slots=True)
class CommitResult:
    request_id: str
    mode: CommitMode
    job: ShareJob | None
    recipients: tuple[ShareRecipient, ...]
    preview: str | None
    already_committed: bool
    """True when this call replayed an existing job rather than creating one
    -- either a cheap early-check hit or a resolved concurrent race."""


class SharingService:
    def __init__(
        self,
        *,
        approval_repo: ApprovalRepository,
        share_repo: ShareRepository,
        scheduler_repo: SchedulerRepository,
        sequences: PostgresSequences,
        task_service: TaskService,
        task_repo: TaskRepository,
        audit: AuditSink,
        tz: ZoneInfo,
        max_message_length: int,
    ) -> None:
        self._approvals = approval_repo
        self._shares = share_repo
        self._scheduler = scheduler_repo
        self._sequences = sequences
        self._task_service = task_service
        self._tasks = task_repo
        self._audit = audit
        self._tz = tz
        self._max_message_length = max_message_length

    def commit(
        self,
        *,
        approval_request_id: str,
        mode: CommitMode,
        action_version: int,
        actor: Actor,
        now: dt.datetime,
        task_updates: Sequence[BulkUpdateItem] = (),
        recipient_group_ids: Sequence[str] = (),
        send_at: dt.datetime | None = None,
        template_id: str | None = None,
        template_source: str | None = None,
    ) -> CommitResult:
        request = self._approvals.get(approval_request_id)
        if request is None:
            raise NotFoundError(
                f"Approval request {approval_request_id} does not exist.",
                request_id=approval_request_id,
            )

        # Layer 1: cheap early check, before any side effect is attempted.
        if mode.produces_a_share:
            existing = self._shares.get_job_by_action_version(
                approval_request_id, action_version
            )
            if existing is not None:
                prior_recipients = self._shares.list_recipients_for_job(existing.id)
                return CommitResult(
                    request_id=approval_request_id,
                    mode=mode,
                    job=existing,
                    recipients=tuple(prior_recipients),
                    preview=None,
                    already_committed=True,
                )

        if request.state not in _COMMITTABLE_STATES:
            raise IllegalTransitionError(
                f"Review {request.display_id} is {request.state.value} and cannot "
                "be committed.",
                request_id=request.id,
                state=request.state.value,
            )

        # Apply task edits as one atomic step: any single conflict aborts the
        # whole commit (raises out of this method, rolling back the caller's
        # transaction) rather than silently landing a partial edit set the
        # user never actually approved as a whole.
        if mode.edits_tasks:
            for item in task_updates:
                self._task_service.update(
                    item.task_id,
                    item.changes,
                    expected_version=item.expected_version,
                    actor=actor,
                )

        if mode is CommitMode.UPDATE_ONLY:
            self._close_no_share(request, actor=actor, now=now)
            return CommitResult(
                request_id=approval_request_id,
                mode=mode,
                job=None,
                recipients=(),
                preview=None,
                already_committed=False,
            )

        if not recipient_group_ids:
            raise NoRecipientsSelectedError(
                "Select at least one WhatsApp group before sharing.",
                request_id=approval_request_id,
            )

        body, snapshot_id, sequence = self._freeze_report(
            approval_request_id=request.id,
            request_kind=request.kind.value,
            now=now,
            template_id=template_id,
            template_source=template_source,
        )

        if request.state is not ApprovalState.APPROVED:
            self._approve(request, actor=actor, now=now)

        run_at = send_at or now
        action = self._scheduler.create(kind=SEND_SHARE_JOB, run_at=run_at, payload={}, now=now)

        job, created = self._shares.create_job(
            sequence=sequence,
            approval_request_id=request.id,
            snapshot_id=snapshot_id,
            approved_by_user_id=actor.id,
            scheduled_action_id=action.id,
            action_version=action_version,
            now=now,
        )

        if not created:
            # Layer 2: lost a genuine race. The winner's job is authoritative;
            # this call's own scheduled_action would otherwise sit orphaned
            # -- PENDING, claimable, but pointing at no share job -- forever.
            self._scheduler.mark_cancelled(action.id, now=now)
            existing_recipients = self._shares.list_recipients_for_job(job.id)
            return CommitResult(
                request_id=approval_request_id,
                mode=mode,
                job=job,
                recipients=tuple(existing_recipients),
                preview=body,
                already_committed=True,
            )

        job_record = transition(
            ShareJobState.PENDING,
            ShareJobState.SCHEDULED,
            entity_type="share_job",
            entity_id=job.id,
            actor=actor,
            at=now,
        )
        self._audit.record(
            action="SHARE_JOB_SCHEDULED",
            entity_type="share_job",
            entity_id=job.id,
            actor=job_record.actor,
            actor_kind=job_record.actor_kind,
            at=now,
            after={"run_at": run_at.isoformat(), "recipient_count": len(recipient_group_ids)},
        )
        scheduled_job = self._shares.save_job_state(
            job.id, state=ShareJobState.SCHEDULED, now=now
        )

        recipients = tuple(
            self._shares.create_recipient(
                share_job_id=job.id, whatsapp_group_id=group_id, now=now
            )
            for group_id in recipient_group_ids
        )

        return CommitResult(
            request_id=approval_request_id,
            mode=mode,
            job=scheduled_job,
            recipients=recipients,
            preview=body,
            already_committed=False,
        )

    def preview(
        self,
        approval_request_id: str,
        *,
        now: dt.datetime,
        template_id: str | None = None,
        template_source: str | None = None,
    ) -> str:
        """Render without approving -- a pure function of current data, safe
        to call repeatedly as the user toggles recipients in the review
        sheet."""
        request = self._approvals.get(approval_request_id)
        if request is None:
            raise NotFoundError(
                f"Approval request {approval_request_id} does not exist.",
                request_id=approval_request_id,
            )
        tasks = self._tasks.list()
        today = local_date(now, self._tz)
        summary = summarize(tasks, today=today, tz=self._tz)
        _default_id, default_source = default_template_for(request.kind.value)
        source = template_source or default_source
        title = REVIEW_TITLES.get(request.kind.value, "Task Update")
        context = build_context(tasks, summary, now=now, tz=self._tz, title=title)
        return render_report(source, context, max_length=self._max_message_length)

    # -- internals ------------------------------------------------------

    def _freeze_report(
        self,
        *,
        approval_request_id: str,
        request_kind: str,
        now: dt.datetime,
        template_id: str | None,
        template_source: str | None,
    ) -> tuple[str, str, int]:
        tasks = self._tasks.list()
        today = local_date(now, self._tz)
        summary = summarize(tasks, today=today, tz=self._tz)

        default_id, default_source = default_template_for(request_kind)
        chosen_id = template_id or default_id
        chosen_source = template_source or default_source
        title = REVIEW_TITLES.get(request_kind, "Task Update")

        context = build_context(tasks, summary, now=now, tz=self._tz, title=title)
        body = render_report(chosen_source, context, max_length=self._max_message_length)

        sequence = self._sequences.next("share")
        snapshot = freeze(
            tasks=tasks,
            summary=summary,
            rendered_body=body,
            template_id=chosen_id,
            now=now,
            tz=self._tz,
            sequence=sequence,
        )
        self._shares.save_snapshot(snapshot, approval_request_id=approval_request_id)
        return body, snapshot.id, sequence

    def _approve(self, request: ApprovalRequest, *, actor: Actor, now: dt.datetime) -> None:
        record = transition(
            request.state,
            ApprovalState.APPROVED,
            entity_type="approval_request",
            entity_id=request.id,
            actor=actor,
            at=now,
        )
        self._audit.record(
            action="REVIEW_APPROVED",
            entity_type="approval_request",
            entity_id=request.id,
            actor=record.actor,
            actor_kind=record.actor_kind,
            at=now,
            before={"state": record.from_state},
            after={"state": record.to_state},
        )
        self._approvals.save_transition(
            request,
            target_state=ApprovalState.APPROVED,
            now=now,
            approved_by_user_id=actor.id,
            approved_at=now,
        )

    def _close_no_share(
        self, request: ApprovalRequest, *, actor: Actor, now: dt.datetime
    ) -> ApprovalRequest:
        record = transition(
            request.state,
            ApprovalState.CLOSED_NO_SHARE,
            entity_type="approval_request",
            entity_id=request.id,
            actor=actor,
            at=now,
        )
        self._audit.record(
            action="REVIEW_CLOSED_NO_SHARE",
            entity_type="approval_request",
            entity_id=request.id,
            actor=record.actor,
            actor_kind=record.actor_kind,
            at=now,
            before={"state": record.from_state},
            after={"state": record.to_state},
        )
        return self._approvals.save_transition(
            request, target_state=ApprovalState.CLOSED_NO_SHARE, now=now
        )
