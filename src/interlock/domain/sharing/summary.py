"""How a report's delivery went, in one small value -- what the Reports list
shows next to each report instead of just "Approved".

Deliberately plain data. Turning it into words ("Sent to 3 groups", "Partly
sent: 2 of 3", "Held back") happens at the edge, where the current time and the
WhatsApp connection are known (``apps/web/src/logic.ts::deliveryResult``).
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from interlock.domain.approvals.states import RecipientState, ShareJobState


@dataclasses.dataclass(frozen=True, slots=True)
class DeliverySummary:
    """The latest share job for one review, and what became of its recipients."""

    job_state: ShareJobState
    run_at: dt.datetime | None
    """When the send was scheduled for (the user's "now" / "in 5 minutes" / "at 21:30")."""
    sent_at: dt.datetime | None
    deferred_reason: str | None
    total: int
    sent: int
    failed: int
    pending: int
    """Still to go: waiting, queued for a retry, or sending right now."""
    skipped: int
    group_names: tuple[str, ...]


def summarize_recipients(states: list[RecipientState]) -> tuple[int, int, int, int, int]:
    """``(total, sent, failed, pending, skipped)`` for one job's recipients."""
    sent = sum(1 for s in states if s is RecipientState.SENT)
    failed = sum(1 for s in states if s is RecipientState.FAILED)
    skipped = sum(1 for s in states if s in {RecipientState.SKIPPED, RecipientState.CANCELLED})
    pending = len(states) - sent - failed - skipped
    return len(states), sent, failed, pending, skipped
