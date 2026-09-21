"""Task entity, derivations and summary counts."""

from __future__ import annotations

import datetime as dt

import pytest

from interlock.domain.common.errors import ValidationFailedError
from interlock.domain.tasks.derivations import (
    derive_flags,
    is_due_soon,
    is_due_today,
    is_overdue,
    sort_key_for_report,
)
from interlock.domain.tasks.entities import (
    Priority,
    TaskStatus,
    apply_edit,
    diff,
)
from interlock.domain.tasks.summary import summarize, summarize_changes
from tests.conftest import IST, make_task

TODAY = dt.date(2026, 9, 15)
YESTERDAY = dt.date(2026, 9, 14)
TOMORROW = dt.date(2026, 9, 16)
NOW = dt.datetime(2026, 9, 15, 17, 4, tzinfo=IST)


class TestOverdueIsDerived:
    def test_past_due_and_unfinished_is_overdue(self) -> None:
        assert is_overdue(YESTERDAY, TaskStatus.PENDING, TODAY)

    def test_completed_is_never_overdue_however_late(self) -> None:
        """Finishing late still counts as finished."""
        assert not is_overdue(dt.date(2020, 1, 1), TaskStatus.COMPLETED, TODAY)

    def test_no_due_date_is_never_overdue(self) -> None:
        assert not is_overdue(None, TaskStatus.PENDING, TODAY)

    def test_due_today_is_not_yet_overdue(self) -> None:
        """You have until the end of the day."""
        assert not is_overdue(TODAY, TaskStatus.PENDING, TODAY)

    def test_blocked_task_can_be_overdue(self) -> None:
        """Being blocked does not stop a deadline passing."""
        assert is_overdue(YESTERDAY, TaskStatus.BLOCKED, TODAY)

    def test_a_due_date_passing_never_changes_status(self) -> None:
        """The brief's explicit rule: never auto-complete on a passed due date.

        Derivation is a pure function of the task -- there is no code path that
        could mutate status, which is the point of deriving rather than storing.
        """
        task = make_task(status=TaskStatus.PENDING, due_date=YESTERDAY)
        flags = derive_flags(task, TODAY)
        assert flags.overdue
        assert task.status is TaskStatus.PENDING


class TestDueBucketsDoNotOverlap:
    def test_due_today_excludes_overdue_and_soon(self) -> None:
        assert is_due_today(TODAY, TaskStatus.PENDING, TODAY)
        assert not is_due_soon(TODAY, TaskStatus.PENDING, TODAY)
        assert not is_overdue(TODAY, TaskStatus.PENDING, TODAY)

    def test_due_soon_excludes_today(self) -> None:
        assert is_due_soon(TOMORROW, TaskStatus.PENDING, TODAY)
        assert not is_due_today(TOMORROW, TaskStatus.PENDING, TODAY)

    def test_beyond_horizon_is_not_due_soon(self) -> None:
        far = TODAY + dt.timedelta(days=10)
        assert not is_due_soon(far, TaskStatus.PENDING, TODAY, horizon_days=2)

    def test_overdue_is_not_due_soon(self) -> None:
        assert not is_due_soon(YESTERDAY, TaskStatus.PENDING, TODAY)


class TestApplyEdit:
    def test_editing_bumps_the_version(self) -> None:
        task = make_task(version=7)
        edited = apply_edit(task, {"title": "Deploy customer server"}, now=NOW)
        assert edited.version == 8
        assert edited.title == "Deploy customer server"

    def test_original_is_untouched(self) -> None:
        """Frozen entities make the before/after pair for history trivial."""
        task = make_task(title="Original")
        apply_edit(task, {"title": "Changed"}, now=NOW)
        assert task.title == "Original"

    def test_marking_complete_stamps_completed_at(self) -> None:
        """Scenario 2: Mark Completed, and the timestamp is recorded for us."""
        task = make_task(status=TaskStatus.PENDING)
        done = apply_edit(task, {"status": TaskStatus.COMPLETED}, now=NOW)
        assert done.status is TaskStatus.COMPLETED
        assert done.completed_at == NOW

    def test_reopening_clears_completed_at(self) -> None:
        """Otherwise a reopened task claims a completion date it does not have."""
        task = make_task(status=TaskStatus.COMPLETED, completed_at=NOW)
        reopened = apply_edit(task, {"status": TaskStatus.IN_PROGRESS}, now=NOW)
        assert reopened.completed_at is None

    def test_editing_a_completed_task_keeps_its_completion_time(self) -> None:
        original_completion = NOW - dt.timedelta(hours=3)
        task = make_task(status=TaskStatus.COMPLETED, completed_at=original_completion)
        edited = apply_edit(task, {"remarks": "Deployment verified"}, now=NOW)
        assert edited.completed_at == original_completion

    def test_system_owned_fields_are_rejected(self) -> None:
        """A typo should fail loudly, not silently drop the edit."""
        task = make_task()
        with pytest.raises(ValidationFailedError) as exc:
            apply_edit(task, {"version": 99}, now=NOW)
        assert "version" in str(exc.value)

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationFailedError):
            apply_edit(make_task(), {"titel": "typo"}, now=NOW)

    def test_empty_edit_is_a_no_op_without_a_version_bump(self) -> None:
        """Saving a form with no changes should not create a history entry."""
        task = make_task(version=4)
        assert apply_edit(task, {}, now=NOW) is task


class TestValidation:
    def test_blank_title_is_rejected(self) -> None:
        with pytest.raises(ValidationFailedError):
            make_task(title="   ")

    def test_naive_created_at_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            make_task(created_at=dt.datetime(2026, 9, 15, 17, 4))  # noqa: DTZ001


class TestDiff:
    def test_diff_reports_only_meaningful_changes(self) -> None:
        before = make_task(title="A", status=TaskStatus.PENDING)
        after = apply_edit(before, {"status": TaskStatus.COMPLETED}, now=NOW)
        changes = diff(before, after)

        assert changes["status"] == {"before": "PENDING", "after": "COMPLETED"}
        # Bookkeeping fields are noise in an audit entry.
        assert "version" not in changes
        assert "updated_at" not in changes


class TestSummary:
    def test_counts_by_status(self) -> None:
        tasks = [
            make_task(status=TaskStatus.COMPLETED),
            make_task(status=TaskStatus.COMPLETED),
            make_task(status=TaskStatus.PENDING),
            make_task(status=TaskStatus.IN_PROGRESS),
            make_task(status=TaskStatus.BLOCKED),
        ]
        summary = summarize(tasks, today=TODAY, tz=IST)

        assert summary.completed == 2
        assert summary.pending == 1
        assert summary.in_progress == 1
        assert summary.blocked == 1
        assert summary.total == 5
        assert summary.remaining == 3

    def test_overdue_cuts_across_status(self) -> None:
        tasks = [
            make_task(status=TaskStatus.PENDING, due_date=YESTERDAY),
            make_task(status=TaskStatus.BLOCKED, due_date=YESTERDAY),
            make_task(status=TaskStatus.COMPLETED, due_date=YESTERDAY),
        ]
        summary = summarize(tasks, today=TODAY, tz=IST)
        assert summary.overdue == 2

    def test_deleted_tasks_are_excluded(self) -> None:
        tasks = [
            make_task(status=TaskStatus.PENDING),
            make_task(status=TaskStatus.PENDING, deleted_at=NOW),
        ]
        assert summarize(tasks, today=TODAY, tz=IST).total == 1

    def test_new_and_modified_are_not_double_counted(self) -> None:
        """A task created and edited today is new, not new *and* modified."""
        created_today = make_task(
            created_at=NOW - dt.timedelta(hours=2),
            updated_at=NOW,
        )
        summary = summarize([created_today], today=TODAY, tz=IST)
        assert summary.newly_added == 1
        assert summary.modified_today == 0


class TestChangeSummary:
    def test_first_ever_share_counts_everything_as_added(self) -> None:
        tasks = [make_task(), make_task()]
        changes = summarize_changes(tasks, since=None, today=TODAY)
        assert changes.added == 2

    def test_changes_since_last_share(self) -> None:
        last_share = NOW - dt.timedelta(hours=8)
        old = make_task(created_at=last_share - dt.timedelta(days=2))
        edited = make_task(
            created_at=last_share - dt.timedelta(days=2),
            updated_at=NOW,
        )
        fresh = make_task(created_at=NOW)

        changes = summarize_changes([old, edited, fresh], since=last_share, today=TODAY)
        assert changes.added == 1
        assert changes.modified == 1


class TestReportOrdering:
    def test_overdue_outranks_priority(self) -> None:
        """"Due yesterday" is more actionable than "marked High"."""
        overdue_low = make_task(priority=Priority.LOW, due_date=YESTERDAY)
        urgent_future = make_task(priority=Priority.URGENT, due_date=TOMORROW)

        ordered = sorted(
            [urgent_future, overdue_low], key=lambda t: sort_key_for_report(t, TODAY)
        )
        assert ordered[0] is overdue_low

    def test_ordering_is_stable_across_renders(self) -> None:
        """Same data must produce the same report twice."""
        tasks = [make_task(priority=Priority.HIGH) for _ in range(5)]
        first = [t.display_id for t in sorted(tasks, key=lambda t: sort_key_for_report(t, TODAY))]
        second = [t.display_id for t in sorted(tasks, key=lambda t: sort_key_for_report(t, TODAY))]
        assert first == second
