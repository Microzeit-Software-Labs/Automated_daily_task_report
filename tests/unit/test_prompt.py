"""Which review the popup asks about -- a pure function of the review rows."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from interlock.domain.approvals.prompt import OPEN_STATES, pick_prompt
from interlock.domain.approvals.request import ApprovalKind, ApprovalRequest
from interlock.domain.approvals.states import ApprovalState
from interlock.domain.common.ids import new_id
from tests.conftest import IST

NINE = dt.datetime(2026, 9, 15, 9, 0, tzinfo=IST)
FIVE_PM = dt.datetime(2026, 9, 15, 17, 0, tzinfo=IST)
MINUTE = dt.timedelta(minutes=1)


def review(
    kind: ApprovalKind = ApprovalKind.EVENING,
    *,
    at: dt.datetime = FIVE_PM,
    state: ApprovalState = ApprovalState.REVIEW_PENDING,
    snoozed_until: dt.datetime | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        id=new_id(),
        display_id=f"REV-{new_id()[-5:]}",
        kind=kind,
        local_date=at.astimezone(IST).date(),
        scheduled_for=at,
        state=state,
        created_at=at,
        updated_at=at,
        snoozed_until=snoozed_until,
    )


def pick(*requests: ApprovalRequest, now: dt.datetime) -> ApprovalRequest | None:
    return pick_prompt(list(requests), now=now, tz=IST)


class TestPrompts:
    def test_nothing_to_prompt_about(self) -> None:
        assert pick(now=FIVE_PM) is None

    @pytest.mark.parametrize("state", sorted(OPEN_STATES))
    def test_an_open_review_prompts(self, state: ApprovalState) -> None:
        evening = review(state=state)
        assert pick(evening, now=FIVE_PM + MINUTE) is evening

    @pytest.mark.parametrize(
        "state",
        [
            ApprovalState.APPROVED,
            ApprovalState.CLOSED_NO_SHARE,
            ApprovalState.CANCELLED,
            ApprovalState.EXPIRED,
        ],
    )
    def test_a_handled_review_does_not(self, state: ApprovalState) -> None:
        assert pick(review(state=state), now=FIVE_PM + MINUTE) is None

    def test_a_manual_review_never_prompts(self) -> None:
        """The user started it themselves; they need no reminder."""
        manual = review(ApprovalKind.MANUAL)
        assert pick(manual, now=FIVE_PM + MINUTE) is None

    def test_yesterdays_review_does_not(self) -> None:
        yesterday = review(at=FIVE_PM - dt.timedelta(days=1))
        assert pick(yesterday, now=FIVE_PM + MINUTE) is None

    def test_a_review_that_has_not_started_yet_does_not(self) -> None:
        assert pick(review(), now=FIVE_PM - MINUTE) is None


class TestLatestWins:
    def test_evening_replaces_an_ignored_morning(self) -> None:
        morning = review(ApprovalKind.MORNING, at=NINE)
        evening = review(ApprovalKind.EVENING, at=FIVE_PM)
        assert pick(morning, evening, now=FIVE_PM + MINUTE) is evening

    def test_a_handled_evening_does_not_resurrect_the_stale_morning(self) -> None:
        """The 09:00 review was ignored; 17:00's is today's question, and once
        it is answered nobody should be asked about the morning one."""
        morning = review(ApprovalKind.MORNING, at=NINE)
        evening = review(ApprovalKind.EVENING, at=FIVE_PM, state=ApprovalState.APPROVED)
        assert pick(morning, evening, now=FIVE_PM + MINUTE) is None

    def test_a_snoozed_evening_does_not_fall_back_to_the_morning(self) -> None:
        morning = review(ApprovalKind.MORNING, at=NINE)
        evening = review(at=FIVE_PM, snoozed_until=FIVE_PM + 30 * MINUTE)
        assert pick(morning, evening, now=FIVE_PM + 5 * MINUTE) is None

    def test_morning_prompts_on_its_own(self) -> None:
        morning = review(ApprovalKind.MORNING, at=NINE)
        assert pick(morning, now=NINE + MINUTE) is morning


class TestSnooze:
    def test_quiet_until_the_snooze_ends(self) -> None:
        snoozed = review(snoozed_until=FIVE_PM + 30 * MINUTE)
        assert pick(snoozed, now=FIVE_PM + 29 * MINUTE) is None

    def test_comes_back_when_it_ends(self) -> None:
        snoozed = review(snoozed_until=FIVE_PM + 30 * MINUTE)
        assert pick(snoozed, now=FIVE_PM + 30 * MINUTE) is snoozed
        assert pick(snoozed, now=FIVE_PM + 45 * MINUTE) is snoozed

    def test_nine_to_half_past_nine(self) -> None:
        """The example from the brief: 9:00 popup -> snooze -> 9:30 popup."""
        opened = review(ApprovalKind.MORNING, at=NINE)
        assert pick(opened, now=NINE) is opened
        snoozed = dataclasses.replace(opened, snoozed_until=NINE + 30 * MINUTE)
        assert pick(snoozed, now=NINE + 10 * MINUTE) is None
        assert pick(snoozed, now=NINE + 30 * MINUTE) is snoozed


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        pick_prompt([], now=dt.datetime(2026, 9, 15, 17, 0), tz=IST)  # noqa: DTZ001
