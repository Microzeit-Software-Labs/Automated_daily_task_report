"""Which review should be asking for the user's attention right now?

At 09:00 and 17:00 the worker opens a review. The popup that asks "send
today's report?" is *derived* from that review -- there is no separate
notification record to lose, duplicate, or forget to clear. As long as the
review is open and not snoozed, it is the prompt; approving, closing or
snoozing it makes the prompt go away, and a snooze bringing it back is just a
timestamp passing. Being a pure function of rows in Postgres, it survives any
restart by construction.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from interlock.domain.approvals.request import ApprovalRequest
from interlock.domain.approvals.states import ApprovalState
from interlock.domain.common.clock import ensure_aware, local_date

OPEN_STATES: frozenset[ApprovalState] = frozenset(
    {ApprovalState.REVIEW_PENDING, ApprovalState.USER_EDITING, ApprovalState.READY}
)
"""States in which a review is still waiting on the user's decision."""

DEFAULT_SNOOZE_MINUTES = 30
MAX_SNOOZE_MINUTES = 240


def pick_prompt(
    requests: Sequence[ApprovalRequest], *, now: dt.datetime, tz: ZoneInfo
) -> ApprovalRequest | None:
    """The review to prompt about, or ``None``.

    Only today's scheduled (morning/evening) reviews count: a MANUAL review is
    one the user started themselves, and needs no reminder. Among those, only
    the *latest* one can prompt -- if the 09:00 review was ignored and the
    17:00 one has since opened, the evening report is today's question, and
    once it is answered nobody should be asked about the stale morning one.
    That latest review prompts while it is open and not snoozed.
    """
    ensure_aware(now, field="now")
    today = local_date(now, tz)
    started = [
        r
        for r in requests
        if r.kind.is_scheduled and r.local_date == today and r.scheduled_for <= now
    ]
    if not started:
        return None

    latest = max(started, key=lambda r: r.scheduled_for)
    if latest.state not in OPEN_STATES:
        return None
    if latest.snoozed_until is not None and latest.snoozed_until > now:
        return None
    return latest
