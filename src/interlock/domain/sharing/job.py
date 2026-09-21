"""ShareJob and ShareRecipient -- an approved send, and its per-group delivery.

Two separate entities on purpose: one review can produce several sends, and
one send fans out to several recipients with independent outcomes. A job's
own state is never assigned directly -- it is rolled up from its recipients
via :func:`interlock.domain.approvals.machine.roll_up_job_state`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from interlock.domain.approvals.states import RecipientState, ShareJobState
from interlock.domain.common.clock import ensure_aware
from interlock.domain.sharing.validity import DeferReason


@dataclasses.dataclass(frozen=True, slots=True)
class ShareJob:
    id: str
    display_id: str
    approval_request_id: str
    snapshot_id: str
    approved_by_user_id: str
    scheduled_action_id: str
    action_version: int
    state: ShareJobState
    created_at: dt.datetime
    updated_at: dt.datetime
    deferred_at: dt.datetime | None = None
    deferred_reason: DeferReason | None = None
    sent_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.created_at, field="share_job.created_at")
        ensure_aware(self.updated_at, field="share_job.updated_at")


@dataclasses.dataclass(frozen=True, slots=True)
class ShareRecipient:
    id: str
    share_job_id: str
    whatsapp_group_id: str
    client_message_id: str
    state: RecipientState
    attempts: int
    created_at: dt.datetime
    updated_at: dt.datetime
    next_attempt_at: dt.datetime | None = None
    provider_message_id: str | None = None
    ack_level: str | None = None
    error_code: str | None = None
    error_detail: str = ""
    sent_at: dt.datetime | None = None
