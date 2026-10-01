"""Request/response models.

``TaskEditFields`` is the one worth reading carefully: every field is typed as
the real domain type (``Priority``, ``TaskStatus``, a real ``date``), not a
bare string. That makes Pydantic itself responsible for coercing
``"COMPLETED"`` into ``TaskStatus.COMPLETED`` at the API boundary --
``model_dump(exclude_unset=True)`` then hands the router a dict of real domain
values, not raw JSON strings a client happened to type. Skipping this and
passing raw strings through to ``apply_edit`` is exactly the bug this project
hit twice already, in its own test fixtures.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interlock.domain.approvals.prompt import DEFAULT_SNOOZE_MINUTES, MAX_SNOOZE_MINUTES
from interlock.domain.approvals.request import ApprovalKind, ApprovalRequest
from interlock.domain.approvals.states import ApprovalState, RecipientState, ShareJobState
from interlock.domain.ports.whatsapp import LinkStatus, PairingState, ProviderStatus, phone_from_jid
from interlock.domain.sharing.group import WhatsAppGroup
from interlock.domain.sharing.job import ShareJob, ShareRecipient
from interlock.domain.sharing.summary import DeliverySummary
from interlock.domain.sync.reconciliation import ConflictResolution, SyncConflict
from interlock.domain.tasks.entities import Priority, Task, TaskSourceKind, TaskStatus
from interlock.domain.tasks.summary import ChangeSummary, TaskSummary
from interlock.services.review_service import ReviewDetail
from interlock.services.sharing_service import CommitMode, CommitResult


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    project_id: str | None = None
    owner_id: str | None = None
    priority: Priority = Priority.MEDIUM
    status: TaskStatus = TaskStatus.PENDING
    due_date: dt.date | None = None
    remarks: str = ""
    tags: tuple[str, ...] = ()


class TaskEditFields(BaseModel):
    """Every field PATCH /tasks/{id} may change. All optional -- only fields
    actually present in the request body are applied, via
    ``model_dump(exclude_unset=True)``, so "not sent" and "explicitly set to
    its current value" are properly distinguishable from "clear this field"."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    project_id: str | None = None
    owner_id: str | None = None
    priority: Priority | None = None
    status: TaskStatus | None = None
    due_date: dt.date | None = None
    remarks: str | None = None
    tags: tuple[str, ...] | None = None


class TaskOut(BaseModel):
    id: str
    display_id: str
    title: str
    description: str
    project_id: str | None
    owner_id: str | None
    priority: Priority
    status: TaskStatus
    created_at: dt.datetime
    updated_at: dt.datetime
    due_date: dt.date | None
    completed_at: dt.datetime | None
    remarks: str
    tags: tuple[str, ...]
    source: TaskSourceKind
    version: int
    last_shared_at: dt.datetime | None

    @classmethod
    def from_entity(cls, task: Task) -> TaskOut:
        return cls(
            id=task.id,
            display_id=task.display_id,
            title=task.title,
            description=task.description,
            project_id=task.project_id,
            owner_id=task.owner_id,
            priority=task.priority,
            status=task.status,
            created_at=task.created_at,
            updated_at=task.updated_at,
            due_date=task.due_date,
            completed_at=task.completed_at,
            remarks=task.remarks,
            tags=task.tags,
            source=task.source,
            version=task.version,
            last_shared_at=task.last_shared_at,
        )


class BulkUpdateItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    changes: TaskEditFields
    expected_version: int = Field(ge=1)


class BulkUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BulkUpdateItemRequest]


class BulkUpdateItemResult(BaseModel):
    task_id: str
    ok: bool
    task: TaskOut | None = None
    error_code: str | None = None
    error_message: str | None = None


class BulkUpdateResponse(BaseModel):
    results: list[BulkUpdateItemResult]


class CommitTaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    changes: TaskEditFields
    expected_version: int = Field(ge=1)


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: CommitMode
    action_version: int = Field(ge=1)
    task_updates: list[CommitTaskUpdate] = Field(default_factory=list)
    recipient_group_ids: list[str] = Field(default_factory=list)
    send_at: dt.datetime | None = None
    delay_minutes: int | None = Field(default=None, ge=1, le=1440)
    """"Send in N minutes", counted by the *server's* clock when the commit
    arrives -- so a wrong or drifting browser clock cannot make "5 minutes"
    mean something else. Mutually exclusive with ``send_at``."""
    template_id: str | None = None
    template_source: str | None = None
    expected_content_hash: str | None = None
    """The ``content_hash`` returned by ``/preview``. When given, the commit is
    refused with ``DATASET_VERSION_CONFLICT`` if the task data has changed
    since that preview -- so the text the user read is the text that goes
    out."""

    @model_validator(mode="after")
    def _one_way_to_schedule(self) -> CommitRequest:
        if self.send_at is not None and self.delay_minutes is not None:
            raise ValueError("Give either send_at or delay_minutes, not both.")
        return self


class ShareRecipientOut(BaseModel):
    id: str
    whatsapp_group_id: str
    state: RecipientState
    attempts: int
    client_message_id: str
    error_code: str | None
    error_detail: str

    @classmethod
    def from_entity(cls, recipient: ShareRecipient) -> ShareRecipientOut:
        return cls(
            id=recipient.id,
            whatsapp_group_id=recipient.whatsapp_group_id,
            state=recipient.state,
            attempts=recipient.attempts,
            client_message_id=recipient.client_message_id,
            error_code=recipient.error_code,
            error_detail=recipient.error_detail,
        )


class ShareJobOut(BaseModel):
    id: str
    display_id: str
    state: ShareJobState
    action_version: int
    deferred_reason: str | None
    sent_at: dt.datetime | None

    @classmethod
    def from_entity(cls, job: ShareJob) -> ShareJobOut:
        return cls(
            id=job.id,
            display_id=job.display_id,
            state=job.state,
            action_version=job.action_version,
            deferred_reason=job.deferred_reason.value if job.deferred_reason else None,
            sent_at=job.sent_at,
        )


class CommitResponse(BaseModel):
    request_id: str
    mode: CommitMode
    job: ShareJobOut | None
    recipients: list[ShareRecipientOut]
    preview: str | None
    already_committed: bool

    @classmethod
    def from_result(cls, result: CommitResult) -> CommitResponse:
        return cls(
            request_id=result.request_id,
            mode=result.mode,
            job=ShareJobOut.from_entity(result.job) if result.job else None,
            recipients=[ShareRecipientOut.from_entity(r) for r in result.recipients],
            preview=result.preview,
            already_committed=result.already_committed,
        )


class PreviewResponse(BaseModel):
    rendered_body: str
    """The whole message in text format; the image's caption in image format."""
    content_hash: str
    has_image: bool = False
    """When true, the report is sent as a table image: fetch it from
    ``GET /approval-requests/{id}/preview.png``."""


class ShareOut(BaseModel):
    """One approved send and how it went, per group -- the review screen's
    delivery panel."""

    job: ShareJobOut
    run_at: dt.datetime | None
    rendered_body: str
    snapshot_id: str
    has_image: bool = False
    """The frozen table image is at ``GET /shares/snapshots/{snapshot_id}/image.png``."""
    recipients: list[ShareRecipientOut]


class ApprovalRequestOut(BaseModel):
    id: str
    display_id: str
    kind: ApprovalKind
    local_date: dt.date
    scheduled_for: dt.datetime
    state: ApprovalState
    opened_at: dt.datetime | None
    approved_by_user_id: str | None
    approved_at: dt.datetime | None
    snoozed_until: dt.datetime | None = None

    @classmethod
    def from_entity(cls, request: ApprovalRequest) -> ApprovalRequestOut:
        return cls(
            id=request.id,
            display_id=request.display_id,
            kind=request.kind,
            local_date=request.local_date,
            scheduled_for=request.scheduled_for,
            state=request.state,
            opened_at=request.opened_at,
            approved_by_user_id=request.approved_by_user_id,
            approved_at=request.approved_at,
            snoozed_until=request.snoozed_until,
        )


class DeliverySummaryOut(BaseModel):
    """How the latest send for a review went, in numbers. The Reports list
    turns it into words ("Sent to 3 groups", "Partly sent 2/3", ...)."""

    job_state: ShareJobState
    run_at: dt.datetime | None
    sent_at: dt.datetime | None
    deferred_reason: str | None
    total: int
    sent: int
    failed: int
    pending: int
    skipped: int
    group_names: list[str]

    @classmethod
    def from_entity(cls, summary: DeliverySummary) -> DeliverySummaryOut:
        return cls(
            job_state=summary.job_state,
            run_at=summary.run_at,
            sent_at=summary.sent_at,
            deferred_reason=summary.deferred_reason,
            total=summary.total,
            sent=summary.sent,
            failed=summary.failed,
            pending=summary.pending,
            skipped=summary.skipped,
            group_names=list(summary.group_names),
        )


class ReviewListItemOut(ApprovalRequestOut):
    """A row of the Reports list: the review, plus how its latest send went
    (null when it was never shared)."""

    delivery: DeliverySummaryOut | None = None


class PromptOut(BaseModel):
    """``prompt`` is the review the popup should ask about, or null."""

    prompt: ApprovalRequestOut | None

    @classmethod
    def from_request(cls, request: ApprovalRequest | None) -> PromptOut:
        return cls(prompt=None if request is None else ApprovalRequestOut.from_entity(request))


class SnoozeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minutes: int = Field(default=DEFAULT_SNOOZE_MINUTES, ge=1, le=MAX_SNOOZE_MINUTES)


class TaskSummaryOut(BaseModel):
    completed: int
    pending: int
    in_progress: int
    blocked: int
    deferred: int
    overdue: int
    due_today: int
    newly_added: int
    modified_today: int
    completed_today: int
    total: int
    remaining: int

    @classmethod
    def from_entity(cls, summary: TaskSummary) -> TaskSummaryOut:
        return cls(**summary.as_dict())


class ChangeSummaryOut(BaseModel):
    added: int
    completed: int
    modified: int
    overdue: int
    still_pending: int

    @classmethod
    def from_entity(cls, changes: ChangeSummary) -> ChangeSummaryOut:
        return cls(**changes.as_dict())


class ReviewDetailOut(BaseModel):
    request: ApprovalRequestOut
    tasks: list[TaskOut]
    summary: TaskSummaryOut
    changes_since_last_share: ChangeSummaryOut
    shares: list[ShareOut] = Field(default_factory=list)

    @classmethod
    def from_detail(
        cls, detail: ReviewDetail, shares: list[ShareOut] | None = None
    ) -> ReviewDetailOut:
        return cls(
            request=ApprovalRequestOut.from_entity(detail.request),
            tasks=[TaskOut.from_entity(t) for t in detail.tasks],
            summary=TaskSummaryOut.from_entity(detail.summary),
            changes_since_last_share=ChangeSummaryOut.from_entity(detail.changes_since_last_share),
            shares=shares or [],
        )


class UiConfigOut(BaseModel):
    """The few settings the browser UI needs to label things correctly."""

    timezone: str
    morning_alert_time: dt.time
    evening_alert_time: dt.time
    working_days: list[int]
    """Weekdays reports are made on, Monday = 0."""
    allow_custom_send_time: bool
    sheet_url: str | None
    user_name: str


class WhatsAppGroupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    external_jid: str = Field(min_length=1, max_length=100)
    description: str = ""
    default_morning: bool = False
    default_evening: bool = False


class WhatsAppGroupOut(BaseModel):
    id: str
    display_name: str
    external_jid: str
    enabled: bool
    description: str
    default_morning: bool
    default_evening: bool
    last_used_at: dt.datetime | None

    @classmethod
    def from_entity(cls, group: WhatsAppGroup) -> WhatsAppGroupOut:
        return cls(
            id=group.id,
            display_name=group.display_name,
            external_jid=group.external_jid,
            enabled=group.enabled,
            description=group.description,
            default_morning=group.default_morning,
            default_evening=group.default_evening,
            last_used_at=group.last_used_at,
        )


class WhatsAppStatusOut(BaseModel):
    provider: str
    state: str
    checked_at: dt.datetime
    last_successful_send_at: dt.datetime | None
    can_send: bool
    detail: str
    reason: str | None = None
    """Why the link is unusable: NOT_LINKED, LOGGED_OUT, SESSION_INVALID,
    REPLACED, FORBIDDEN or AGENT_OFFLINE. Null while healthy or reconnecting."""
    account_jid: str | None = None
    account_number: str | None = None
    """The linked phone, ``+919876543210``; kept after a disconnect."""
    account_name: str | None = None

    @classmethod
    def from_status(cls, status: ProviderStatus) -> WhatsAppStatusOut:
        return cls(
            provider=status.provider,
            state=status.state.value,
            checked_at=status.checked_at,
            last_successful_send_at=status.last_successful_send_at,
            can_send=status.can_send,
            detail=status.detail,
            reason=status.reason,
            account_jid=status.account_jid,
            account_number=phone_from_jid(status.account_jid),
            account_name=status.account_name,
        )


class LinkStatusOut(BaseModel):
    """Progress of linking a phone. The QR itself is an image at
    ``GET /whatsapp/link/qr.svg`` (``qr_version`` changes whenever it does, so
    a page can cache-bust on it)."""

    state: PairingState
    detail: str
    pairing_id: str | None
    has_qr: bool
    qr_version: str | None
    agent_online: bool

    @classmethod
    def from_status(cls, status: LinkStatus) -> LinkStatusOut:
        return cls(
            state=status.state,
            detail=status.detail,
            pairing_id=status.pairing_id,
            has_qr=status.qr is not None,
            qr_version=status.qr_at.isoformat() if status.qr_at else None,
            agent_online=status.agent_online,
        )


class GroupCandidateOut(BaseModel):
    external_jid: str
    display_name: str
    member_count: int | None
    confidence: float


class ResolveGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)


class SyncConflictOut(BaseModel):
    id: str
    task_id: str
    detected_at: dt.datetime
    db_value: dict[str, Any]
    external_value: dict[str, Any] | None
    resolution: str | None
    resolved_by: str | None
    resolved_at: dt.datetime | None
    is_open: bool

    @classmethod
    def from_entity(cls, conflict: SyncConflict) -> SyncConflictOut:
        return cls(
            id=conflict.id,
            task_id=conflict.task_id,
            detected_at=conflict.detected_at,
            db_value=conflict.db_value,
            external_value=conflict.external_value,
            resolution=conflict.resolution,
            resolved_by=conflict.resolved_by,
            resolved_at=conflict.resolved_at,
            is_open=conflict.is_open,
        )


class ResolveConflictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: ConflictResolution


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}
    retryable: bool = False


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
