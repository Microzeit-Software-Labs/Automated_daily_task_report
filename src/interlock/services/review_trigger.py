"""The 09:00 / 17:00 trigger.

This is the other half of "durable scheduling" alongside ``SchedulerService``:
that module claims and sends already-approved reports; this one decides
*when a review should exist in the first place*. It is deliberately not a
``scheduled_actions`` row with a fixed due time, because the alert times are
recurring wall-clock configuration (``MORNING_ALERT_TIME=09:00``), not a
one-shot future event -- there is no single instant to schedule against, only
a daily check of "has today's happened yet".

Called every worker tick, alongside ``SchedulerService.tick()``. Creating a
review is already idempotent (``ReviewService.create_scheduled_review`` /
``ApprovalRepository.create_scheduled``), so calling this once a minute or
once a second changes nothing about correctness -- a review for
``(kind, today)`` is created at most once regardless of how many times this
function notices that it is time.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

from interlock.domain.approvals.request import ApprovalKind, ApprovalRequest
from interlock.domain.common.clock import combine_local, local_date
from interlock.services.review_service import ReviewService


@dataclasses.dataclass(frozen=True, slots=True)
class TriggerResult:
    kind: ApprovalKind
    request: ApprovalRequest
    newly_created: bool


def maybe_create_daily_reviews(
    review_service: ReviewService,
    *,
    now: dt.datetime,
    tz: ZoneInfo,
    working_days: frozenset[int],
    morning_alert_time: dt.time,
    evening_alert_time: dt.time,
) -> list[TriggerResult]:
    """Create today's morning and/or evening review if their alert time has
    passed and none exists yet. Returns one ``TriggerResult`` per kind whose
    alert time has passed today, whether or not this call is the one that
    actually created it -- ``newly_created`` distinguishes the two, which is
    what a notification step uses to avoid re-notifying for a review that
    already existed.
    """
    today = local_date(now, tz)
    if today.weekday() not in working_days:
        return []

    results: list[TriggerResult] = []
    for kind, alert_time in (
        (ApprovalKind.MORNING, morning_alert_time),
        (ApprovalKind.EVENING, evening_alert_time),
    ):
        scheduled_for = combine_local(today, alert_time, tz)
        if now < scheduled_for:
            continue
        request, created = review_service.create_scheduled_review(
            kind, scheduled_for=scheduled_for, now=now
        )
        results.append(TriggerResult(kind=kind, request=request, newly_created=created))

    return results
