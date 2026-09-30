"""Is this frozen report still safe to send unattended?

Evaluated at claim time, immediately before dispatch. This is the rule that
stops a laptop waking at 08:00 and cheerfully sending yesterday's "End of Day"
report as if it were today's.

**Measured from when the report was meant to go out.** That is the approval
time for "send now", and the chosen time for "send later" -- whichever is
later. A 17:00 report delivered at 17:06 is fine: that is just a laptop waking
up late, and it is the common case. The same report delivered at 08:00 the
next morning is actively misleading. A report the user deliberately scheduled
for 21:30 is *meant* for 21:30, so it may go out then -- judging it by its
17:00 approval would hold back every "send later" beyond the grace window.
The calendar day of the intended time is a hard boundary regardless of grace.

Every check is a pure function of its arguments so that each branch is directly
testable without a database or a clock.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from enum import StrEnum
from zoneinfo import ZoneInfo

from interlock.domain.approvals.states import ShareJobState
from interlock.domain.common.clock import ensure_aware, local_date


class Verdict(StrEnum):
    SEND = "SEND"
    """Still valid. Dispatch now."""

    DEFER = "DEFER"
    """No longer valid to send unattended. Park it and ask the user."""

    DROP = "DROP"
    """Not in a dispatchable state at all -- already cancelled, already sent, or
    picked up by another worker. Not an error; just stop."""

    SUPERSEDE = "SUPERSEDE"
    """A newer approved report for the same review replaced this one."""


class DeferReason(StrEnum):
    CROSSED_DAY_BOUNDARY = "CROSSED_DAY_BOUNDARY"
    SNAPSHOT_STALE = "SNAPSHOT_STALE"
    DATA_DRIFTED = "DATA_DRIFTED"
    WHATSAPP_DISCONNECTED = "WHATSAPP_DISCONNECTED"
    """Stale or past its day for the usual reasons -- but specifically
    because WhatsApp was disconnected the whole time it could have gone out.
    Set by the scheduler, not by this module: validity itself is
    provider-agnostic."""


_DEFER_MESSAGES: dict[DeferReason, str] = {
    DeferReason.CROSSED_DAY_BOUNDARY: (
        "it was approved on a different day and would have been sent with the wrong date"
    ),
    DeferReason.SNAPSHOT_STALE: "it sat unsent for longer than the grace window",
    DeferReason.DATA_DRIFTED: "your task data changed after you approved it",
    DeferReason.WHATSAPP_DISCONNECTED: (
        "WhatsApp was disconnected until it was too late to send it"
    ),
}


@dataclasses.dataclass(frozen=True, slots=True)
class ValidityDecision:
    verdict: Verdict
    reason: DeferReason | None = None
    detail: str = ""

    @property
    def should_send(self) -> bool:
        return self.verdict is Verdict.SEND

    def user_message(self, *, label: str) -> str:
        """The notification text shown when a send is deferred."""
        if self.reason is None:
            return f"{label} was not sent."
        return (
            f"{label} was not sent because {_DEFER_MESSAGES[self.reason]}. "
            "Review it and send, or discard it."
        )


def intended_send_time(
    snapshot_created_at: dt.datetime, run_at: dt.datetime | None
) -> dt.datetime:
    """When a report was meant to go out: its approval for "send now", its
    chosen time for "send later" -- whichever is later, so a ``run_at`` a
    moment in the past never makes a fresh approval look late."""
    ensure_aware(snapshot_created_at, field="snapshot.created_at")
    if run_at is None:
        return snapshot_created_at
    ensure_aware(run_at, field="run_at")
    return max(snapshot_created_at, run_at)


def missed_its_day(
    *,
    snapshot_created_at: dt.datetime,
    run_at: dt.datetime | None,
    now: dt.datetime,
    tz: ZoneInfo,
) -> bool:
    """Has the local calendar day this report was meant for already ended?

    The scheduler checks this before an automatic *retry*: a retry waiting out
    a long WhatsApp outage must never deliver yesterday's report once the
    connection comes back. (A human pressing Retry is not held to it.)
    """
    ensure_aware(now, field="now")
    intended_on = local_date(intended_send_time(snapshot_created_at, run_at), tz)
    return local_date(now, tz) > intended_on


SEND = ValidityDecision(Verdict.SEND)
DROP = ValidityDecision(Verdict.DROP)


def evaluate_send_validity(
    *,
    job_state: ShareJobState,
    snapshot_created_at: dt.datetime,
    snapshot_content_hash: str,
    current_content_hash: str | None,
    now: dt.datetime,
    tz: ZoneInfo,
    grace: dt.timedelta,
    enforce_day_boundary: bool = True,
    strict_data_drift: bool = False,
    superseded_by: str | None = None,
    run_at: dt.datetime | None = None,
) -> ValidityDecision:
    """Decide what to do with a share job that has come due.

    Args:
        job_state: current state of the job being claimed.
        snapshot_created_at: when the report was frozen, i.e. when the user
            approved it.
        snapshot_content_hash: hash of the task data captured in the snapshot.
        current_content_hash: hash of the task data right now, or ``None`` if
            drift checking is not available.
        now: current instant.
        tz: the user's timezone -- the calendar day is defined in local terms.
        grace: how stale a report may be and still send unattended.
        enforce_day_boundary: hard-stop on crossing midnight, checked ahead of
            ``grace``.
        strict_data_drift: defer if task data changed since approval. Off by
            default, because snapshots exist precisely so drift is harmless.
        superseded_by: id of a newer approved job for the same review, if any.
        run_at: when the user asked for it to go out ("in 5 minutes", "at
            21:30", "tomorrow 09:00"). ``None`` means "as soon as approved".

    Both time checks are measured from the *intended* send time -- the later
    of approval and ``run_at``. An immediate send intends to go out at
    approval, so for it this is exactly "staleness since approval". A send
    the user deliberately scheduled for 21:30 intends 21:30: it must go out
    on that day and within ``grace`` of 21:30, not within ``grace`` of the
    17:00 approval -- otherwise every "send later" beyond the grace window
    would be held back as stale the moment it came due.
    """
    ensure_aware(now, field="now")
    intended_at = intended_send_time(snapshot_created_at, run_at)

    # --- 1. Structural. Cheapest, and the only checks that are not about time.
    if job_state not in _DISPATCHABLE_STATES:
        return ValidityDecision(
            Verdict.DROP,
            detail=f"job is in state {job_state.value}, which is not dispatchable",
        )

    if superseded_by is not None:
        return ValidityDecision(
            Verdict.SUPERSEDE,
            detail=f"replaced by a newer approved report ({superseded_by})",
        )

    # --- 2. Day boundary. Deliberately BEFORE the grace check.
    # A report approved at 23:55 with a 60-minute grace would otherwise be sent
    # at 00:30 the next day, carrying yesterday's date in its header. For a
    # report scheduled for a later day, "its day" is the day it was scheduled
    # for: it may go out then, and never on the day after.
    if enforce_day_boundary:
        intended_on = local_date(intended_at, tz)
        today = local_date(now, tz)
        if intended_on != today:
            return ValidityDecision(
                Verdict.DEFER,
                DeferReason.CROSSED_DAY_BOUNDARY,
                detail=f"meant for {intended_on.isoformat()}, now {today.isoformat()}",
            )

    # --- 3. Staleness grace, from the intended send time.
    late_by = now - intended_at
    if late_by > grace:
        return ValidityDecision(
            Verdict.DEFER,
            DeferReason.SNAPSHOT_STALE,
            detail=(
                f"{_humanise(late_by)} past its send time, grace is {_humanise(grace)}"
            ),
        )

    # --- 4. Data drift. Advisory, off by default.
    if (
        strict_data_drift
        and current_content_hash is not None
        and current_content_hash != snapshot_content_hash
    ):
        return ValidityDecision(
            Verdict.DEFER,
            DeferReason.DATA_DRIFTED,
            detail="task data changed after approval",
        )

    return SEND


_DISPATCHABLE_STATES: frozenset[ShareJobState] = frozenset(
    {
        ShareJobState.PENDING,
        ShareJobState.SCHEDULED,
        ShareJobState.DEFERRED,
        # DEFERRED is dispatchable because a human can explicitly re-send one.
        # The scheduler never claims a DEFERRED job on its own -- its run_at is
        # cleared when it defers.
    }
)


def is_expired_deferral(
    *,
    deferred_at: dt.datetime,
    now: dt.datetime,
    ttl: dt.timedelta,
) -> bool:
    """Has a deferred job sat unanswered long enough to auto-cancel?

    Keeps the Scheduled Shares list from filling with stale reports nobody will
    ever send. The cancellation is audited, never silent.
    """
    ensure_aware(now, field="now")
    ensure_aware(deferred_at, field="deferred_at")
    return now - deferred_at > ttl


def _humanise(delta: dt.timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    hours, minutes = divmod(total // 60, 60)
    return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
