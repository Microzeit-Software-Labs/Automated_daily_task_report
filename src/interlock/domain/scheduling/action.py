"""ScheduledAction -- the durable queue row a worker claims and executes.

This table's own ``status`` (PENDING/CLAIMED/DONE/FAILED/CANCELLED) answers
"has a worker claimed and finished this row" -- never "what was the business
outcome", which lives on whatever the payload points at (a ShareJob's own
state, most often). Conflating the two would make "2 sent, 1 failed, retry
only the failed one" unrepresentable at this layer.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from enum import StrEnum
from typing import Any

from interlock.domain.common.clock import ensure_aware

SEND_SHARE_JOB: str = "SEND_SHARE_JOB"
"""The one action kind Phase 1 schedules. Left as a plain string constant
rather than an enum with a single member -- a second kind (e.g. a nightly
audit-chain check) can be added later without touching this one."""


class ActionStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclasses.dataclass(frozen=True, slots=True)
class ScheduledAction:
    id: str
    kind: str
    run_at: dt.datetime
    payload: dict[str, Any]
    status: ActionStatus
    attempts: int
    created_at: dt.datetime
    updated_at: dt.datetime
    claimed_at: dt.datetime | None = None
    claimed_by: str | None = None
    lease_until: dt.datetime | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.run_at, field="scheduled_action.run_at")
        ensure_aware(self.created_at, field="scheduled_action.created_at")
        ensure_aware(self.updated_at, field="scheduled_action.updated_at")
