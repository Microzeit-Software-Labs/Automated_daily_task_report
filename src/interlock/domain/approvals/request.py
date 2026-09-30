"""The ApprovalRequest entity -- one scheduled or manual review."""

from __future__ import annotations

import dataclasses
import datetime as dt
from enum import StrEnum

from interlock.domain.approvals.states import ApprovalState
from interlock.domain.common.clock import ensure_aware


class ApprovalKind(StrEnum):
    MORNING = "MORNING"
    EVENING = "EVENING"
    MANUAL = "MANUAL"

    @property
    def is_scheduled(self) -> bool:
        """Whether the duplicate-once-per-day guard applies to this kind.

        A MANUAL review ("share the list right now") is legitimately allowed
        more than once a day; only the two scheduled kinds are constrained.
        """
        return self is not ApprovalKind.MANUAL


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    display_id: str
    kind: ApprovalKind
    local_date: dt.date
    scheduled_for: dt.datetime
    state: ApprovalState
    created_at: dt.datetime
    updated_at: dt.datetime
    opened_at: dt.datetime | None = None
    dataset_version_at_open: str | None = None
    approved_by_user_id: str | None = None
    approved_at: dt.datetime | None = None
    snoozed_until: dt.datetime | None = None
    """"Remind me later": the popup stays quiet until this passes. Not part of
    the approval state machine -- snoozing changes no state."""

    def __post_init__(self) -> None:
        ensure_aware(self.scheduled_for, field="approval_request.scheduled_for")
        ensure_aware(self.created_at, field="approval_request.created_at")
        ensure_aware(self.updated_at, field="approval_request.updated_at")
        if self.snoozed_until is not None:
            ensure_aware(self.snoozed_until, field="approval_request.snoozed_until")
