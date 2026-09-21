"""The missed-window rule.

This is the rule that decides whether a laptop waking up late may send a report
that was frozen hours ago. Every branch is covered, because getting this wrong
means either silently dropping reports or sending yesterday's news as today's.
"""

from __future__ import annotations

import datetime as dt

import pytest

from interlock.domain.approvals.states import ShareJobState
from interlock.domain.sharing.validity import (
    DeferReason,
    Verdict,
    evaluate_send_validity,
    is_expired_deferral,
)
from tests.conftest import IST

GRACE = dt.timedelta(minutes=60)
HASH_A = "a" * 64
HASH_B = "b" * 64


def evaluate(**overrides: object):
    """Defaults describe the healthy case: approved at 17:04, sending at 17:09."""
    approved = dt.datetime(2026, 9, 15, 17, 4, tzinfo=IST)
    params = {
        "job_state": ShareJobState.SCHEDULED,
        "snapshot_created_at": approved,
        "snapshot_content_hash": HASH_A,
        "current_content_hash": HASH_A,
        "now": approved + dt.timedelta(minutes=5),
        "tz": IST,
        "grace": GRACE,
    }
    params.update(overrides)
    return evaluate_send_validity(**params)  # type: ignore[arg-type]


class TestSends:
    def test_on_time_send_is_valid(self) -> None:
        assert evaluate().verdict is Verdict.SEND

    def test_laptop_woke_four_minutes_late_still_sends(self) -> None:
        """The common case. Lateness alone must never block a send."""
        approved = dt.datetime(2026, 9, 15, 17, 4, tzinfo=IST)
        decision = evaluate(now=approved + dt.timedelta(minutes=4))
        assert decision.verdict is Verdict.SEND

    def test_send_at_exactly_the_grace_boundary_is_allowed(self) -> None:
        """Boundary is inclusive: 60 minutes is within a 60-minute grace."""
        approved = dt.datetime(2026, 9, 15, 17, 4, tzinfo=IST)
        decision = evaluate(now=approved + GRACE)
        assert decision.verdict is Verdict.SEND

    def test_drift_is_ignored_when_strict_mode_is_off(self) -> None:
        """Snapshots exist so drift is harmless. Default must not defer on it."""
        decision = evaluate(current_content_hash=HASH_B)
        assert decision.verdict is Verdict.SEND


class TestDefers:
    def test_beyond_grace_defers_as_stale(self) -> None:
        approved = dt.datetime(2026, 9, 15, 17, 4, tzinfo=IST)
        decision = evaluate(now=approved + GRACE + dt.timedelta(seconds=1))
        assert decision.verdict is Verdict.DEFER
        assert decision.reason is DeferReason.SNAPSHOT_STALE

    def test_next_morning_defers(self) -> None:
        """Scenario 5 gone wrong: laptop asleep all night."""
        approved = dt.datetime(2026, 9, 15, 17, 4, tzinfo=IST)
        next_morning = dt.datetime(2026, 9, 16, 8, 0, tzinfo=IST)
        decision = evaluate(snapshot_created_at=approved, now=next_morning)
        assert decision.verdict is Verdict.DEFER
        assert decision.reason is DeferReason.CROSSED_DAY_BOUNDARY

    def test_day_boundary_beats_grace(self) -> None:
        """A 23:55 approval must not send at 00:30 with yesterday's date.

        Only 35 minutes have passed, comfortably inside the 60-minute grace, so
        this can only defer if the day check runs first. This test is the reason
        the checks are ordered the way they are.
        """
        approved = dt.datetime(2026, 9, 15, 23, 55, tzinfo=IST)
        just_after_midnight = dt.datetime(2026, 9, 16, 0, 30, tzinfo=IST)
        assert just_after_midnight - approved < GRACE

        decision = evaluate(snapshot_created_at=approved, now=just_after_midnight)
        assert decision.verdict is Verdict.DEFER
        assert decision.reason is DeferReason.CROSSED_DAY_BOUNDARY

    def test_day_boundary_uses_local_time_not_utc(self) -> None:
        """IST is UTC+5:30, so a local day rolls over mid-afternoon UTC.

        Judging the calendar day in UTC would defer a perfectly valid evening
        report every single day.
        """
        approved = dt.datetime(2026, 9, 15, 20, 0, tzinfo=IST)  # 14:30 UTC
        five_min_later = approved + dt.timedelta(minutes=5)  # still the 15th in IST
        decision = evaluate(snapshot_created_at=approved, now=five_min_later)
        assert decision.verdict is Verdict.SEND

    def test_strict_drift_defers_when_enabled(self) -> None:
        decision = evaluate(current_content_hash=HASH_B, strict_data_drift=True)
        assert decision.verdict is Verdict.DEFER
        assert decision.reason is DeferReason.DATA_DRIFTED

    def test_day_boundary_can_be_disabled(self) -> None:
        approved = dt.datetime(2026, 9, 15, 23, 55, tzinfo=IST)
        decision = evaluate(
            snapshot_created_at=approved,
            now=dt.datetime(2026, 9, 16, 0, 30, tzinfo=IST),
            enforce_day_boundary=False,
        )
        assert decision.verdict is Verdict.SEND


class TestStructural:
    @pytest.mark.parametrize(
        "state",
        [
            ShareJobState.SENT,
            ShareJobState.CANCELLED,
            ShareJobState.SENDING,
            ShareJobState.SUPERSEDED,
        ],
    )
    def test_undispatchable_states_drop(self, state: ShareJobState) -> None:
        """Already handled, or being handled by another worker. Not an error."""
        assert evaluate(job_state=state).verdict is Verdict.DROP

    def test_superseded_job_does_not_send(self) -> None:
        decision = evaluate(superseded_by="SHR-00205")
        assert decision.verdict is Verdict.SUPERSEDE

    def test_structural_checks_run_before_time_checks(self) -> None:
        """A cancelled job that is also stale reports DROP, not DEFER.

        Nothing should ask the user to review a job they already cancelled.
        """
        decision = evaluate(
            job_state=ShareJobState.CANCELLED,
            now=dt.datetime(2026, 9, 20, 9, 0, tzinfo=IST),
        )
        assert decision.verdict is Verdict.DROP


class TestNaiveDatetimesRejected:
    def test_naive_now_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            evaluate(now=dt.datetime(2026, 9, 15, 17, 9))  # noqa: DTZ001

    def test_naive_snapshot_time_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            evaluate(snapshot_created_at=dt.datetime(2026, 9, 15, 17, 4))  # noqa: DTZ001


class TestDeferralExpiry:
    def test_fresh_deferral_is_not_expired(self) -> None:
        now = dt.datetime(2026, 9, 16, 9, 0, tzinfo=IST)
        assert not is_expired_deferral(
            deferred_at=now - dt.timedelta(hours=2),
            now=now,
            ttl=dt.timedelta(hours=48),
        )

    def test_old_deferral_expires(self) -> None:
        now = dt.datetime(2026, 9, 20, 9, 0, tzinfo=IST)
        assert is_expired_deferral(
            deferred_at=now - dt.timedelta(hours=72),
            now=now,
            ttl=dt.timedelta(hours=48),
        )


class TestUserMessaging:
    def test_defer_message_explains_the_reason(self) -> None:
        approved = dt.datetime(2026, 9, 15, 17, 4, tzinfo=IST)
        decision = evaluate(now=dt.datetime(2026, 9, 16, 8, 0, tzinfo=IST),
                            snapshot_created_at=approved)
        message = decision.user_message(label="Your 5:00 PM report")

        assert "Your 5:00 PM report" in message
        assert "wrong date" in message
        assert "Review it and send, or discard it." in message
