"""Report rendering and snapshot freezing."""

from __future__ import annotations

import datetime as dt

import pytest

from interlock.domain.common.errors import MessageTooLongError, TemplateInvalidError
from interlock.domain.sharing.render import (
    DEFAULT_EVENING_TEMPLATE,
    DEFAULT_MORNING_TEMPLATE,
    EVENING_TEMPLATE_ID,
    build_context,
    render_report,
    validate_template,
)
from interlock.domain.sharing.snapshot import (
    compute_content_hash,
    format_dataset_version,
    freeze,
)
from interlock.domain.tasks.entities import Priority, TaskStatus, apply_edit
from interlock.domain.tasks.summary import summarize
from tests.conftest import IST, make_task

NOW = dt.datetime(2026, 9, 15, 17, 4, tzinfo=IST)
TODAY = dt.date(2026, 9, 15)
YESTERDAY = dt.date(2026, 9, 14)


def sample_tasks() -> list:
    return [
        make_task(title="Deploy production server", status=TaskStatus.COMPLETED, completed_at=NOW),
        make_task(title="Customer integration", status=TaskStatus.IN_PROGRESS),
        make_task(title="Database migration", status=TaskStatus.PENDING,
                  due_date=YESTERDAY, priority=Priority.HIGH),
    ]


def render_default(template: str = DEFAULT_EVENING_TEMPLATE) -> str:
    tasks = sample_tasks()
    summary = summarize(tasks, today=TODAY, tz=IST)
    context = build_context(tasks, summary, now=NOW, tz=IST)
    return render_report(template, context, max_length=4096)


class TestTemplateValidation:
    @pytest.mark.parametrize(
        "template", [DEFAULT_MORNING_TEMPLATE, DEFAULT_EVENING_TEMPLATE]
    )
    def test_shipped_templates_are_valid(self, template: str) -> None:
        validate_template(template)

    def test_unknown_variable_is_rejected_at_save_time(self) -> None:
        """Better to fail when editing than at 17:00 when a report is due."""
        with pytest.raises(TemplateInvalidError) as exc:
            validate_template("Hello {{ nonexistent_field }}")
        assert "nonexistent_field" in str(exc.value)

    def test_syntax_error_is_rejected(self) -> None:
        with pytest.raises(TemplateInvalidError):
            validate_template("{% for task in tasks %}unclosed")


class TestSandbox:
    def test_builtins_are_not_reachable(self) -> None:
        """env.globals is cleared, so range() and friends are undefined."""
        with pytest.raises(TemplateInvalidError):
            validate_template("{{ range(10) }}")

    def test_private_attribute_access_is_blocked(self) -> None:
        tasks = sample_tasks()
        summary = summarize(tasks, today=TODAY, tz=IST)
        context = build_context(tasks, summary, now=NOW, tz=IST)
        with pytest.raises(TemplateInvalidError):
            render_report("{{ tasks.__class__ }}", context, max_length=4096)


class TestRendering:
    def test_evening_report_contains_the_headline_counts(self) -> None:
        body = render_default()
        assert "*End of Day Task Update*" in body
        assert "Completed today: 1" in body
        assert "In Progress: 1" in body
        assert "Pending: 1" in body

    def test_date_and_time_are_local(self) -> None:
        """17:04 IST must not render as 11:34 UTC."""
        body = render_default()
        assert "15 September 2026" in body
        assert "5:04 PM" in body

    def test_overdue_task_is_marked(self) -> None:
        body = render_default()
        assert "overdue" in body

    def test_tasks_are_listed_most_urgent_first(self) -> None:
        body = render_default()
        # Database migration is overdue, so it leads regardless of priority.
        assert body.index("Database migration") < body.index("Deploy production server")

    def test_output_has_no_runaway_blank_lines(self) -> None:
        assert "\n\n\n" not in render_default()

    @pytest.mark.parametrize("template", [DEFAULT_MORNING_TEMPLATE, DEFAULT_EVENING_TEMPLATE])
    def test_every_task_starts_on_its_own_line(self, template: str) -> None:
        """Regression: an {% endif %} ending each status line swallowed the
        newline (trim_blocks), gluing "In Progress" to the next task's
        number -- in every report ever sent."""
        lines = render_default(template).splitlines()
        for number, title in enumerate(
            ["Database migration", "Customer integration", "Deploy production server"], start=1
        ):
            assert f"{number}. {title}" in lines
        assert not any(line.rstrip().endswith(("Progress2.", "Pending2.")) for line in lines)
        assert "   \U0001f534 Pending — overdue" in lines

    def test_a_task_completed_on_an_earlier_day_is_not_listed(self) -> None:
        """With months of history in the task list, "top 15 by urgency" would
        otherwise be 15 long-closed tasks."""
        old = make_task(
            title="Closed back in April",
            status=TaskStatus.COMPLETED,
            completed_at=NOW - dt.timedelta(days=150),
        )
        tasks = [*sample_tasks(), old]
        context = build_context(tasks, summarize(tasks, today=TODAY, tz=IST), now=NOW, tz=IST)
        titles = [task["title"] for task in context["tasks"]]
        assert "Closed back in April" not in titles
        assert "Deploy production server" in titles  # completed today: still listed

    def test_completed_today_sorts_after_every_open_task(self) -> None:
        tasks = sample_tasks()
        context = build_context(tasks, summarize(tasks, today=TODAY, tz=IST), now=NOW, tz=IST)
        assert context["tasks"][-1]["title"] == "Deploy production server"

    def test_completed_today_counts_only_today(self) -> None:
        tasks = [
            *sample_tasks(),
            make_task(
                title="Old", status=TaskStatus.COMPLETED, completed_at=NOW - dt.timedelta(days=2)
            ),
        ]
        summary = summarize(tasks, today=TODAY, tz=IST)
        assert summary.completed_today == 1
        assert summary.completed == 2  # the all-time count is unchanged

    def test_overlong_report_fails_loudly(self) -> None:
        """Truncating a report silently is worse than refusing to send it."""
        tasks = sample_tasks()
        summary = summarize(tasks, today=TODAY, tz=IST)
        context = build_context(tasks, summary, now=NOW, tz=IST)
        with pytest.raises(MessageTooLongError):
            render_report(DEFAULT_EVENING_TEMPLATE, context, max_length=50)

    def test_task_list_is_capped(self) -> None:
        many = [make_task(title=f"Task {i}") for i in range(60)]
        summary = summarize(many, today=TODAY, tz=IST)
        context = build_context(many, summary, now=NOW, tz=IST, max_tasks=15)
        assert len(context["tasks"]) == 15
        # Counts still reflect everything, not just what was listed.
        assert context["total"] == 60


class TestContentHash:
    def test_identical_data_hashes_identically(self) -> None:
        tasks = sample_tasks()
        assert compute_content_hash(tasks) == compute_content_hash(list(tasks))

    def test_hash_is_order_independent(self) -> None:
        """Re-sorting a list must not look like a data change."""
        tasks = sample_tasks()
        assert compute_content_hash(tasks) == compute_content_hash(list(reversed(tasks)))

    def test_status_change_changes_the_hash(self) -> None:
        tasks = sample_tasks()
        before = compute_content_hash(tasks)
        tasks[2] = apply_edit(tasks[2], {"status": TaskStatus.COMPLETED}, now=NOW)
        assert compute_content_hash(tasks) != before

    def test_touching_a_task_without_changing_it_does_not_drift(self) -> None:
        """A re-save with no visible change should not register as drift."""
        tasks = sample_tasks()
        before = compute_content_hash(tasks)
        tasks[0] = apply_edit(tasks[0], {"title": tasks[0].title}, now=NOW)
        assert compute_content_hash(tasks) == before

    def test_deleted_tasks_are_excluded(self) -> None:
        tasks = sample_tasks()
        baseline = compute_content_hash(tasks)
        tasks.append(make_task(title="Removed", deleted_at=NOW))
        assert compute_content_hash(tasks) == baseline


class TestDatasetVersion:
    def test_format_matches_the_specified_shape(self) -> None:
        assert format_dataset_version(NOW, IST, 12) == "2026-09-15-17-04-v12"

    def test_version_uses_local_time(self) -> None:
        """A 20:00 IST approval is 14:30 UTC -- the label must say 20-00."""
        evening = dt.datetime(2026, 9, 15, 20, 0, tzinfo=IST)
        assert format_dataset_version(evening, IST, 1).startswith("2026-09-15-20-00")


class TestFreeze:
    def build(self):
        tasks = sample_tasks()
        summary = summarize(tasks, today=TODAY, tz=IST)
        body = render_default()
        return tasks, freeze(
            tasks=tasks,
            summary=summary,
            rendered_body=body,
            template_id=EVENING_TEMPLATE_ID,
            now=NOW,
            tz=IST,
            sequence=12,
        )

    def test_snapshot_captures_the_rendered_body(self) -> None:
        _, snapshot = self.build()
        assert "*End of Day Task Update*" in snapshot.rendered_body
        assert snapshot.dataset_version == "2026-09-15-17-04-v12"
        assert snapshot.task_count == 3

    def test_snapshot_is_immutable(self) -> None:
        """The whole guarantee: what was approved cannot be edited afterwards."""
        _, snapshot = self.build()
        with pytest.raises(AttributeError):
            snapshot.rendered_body = "tampered"  # type: ignore[misc]

    def test_later_task_edits_do_not_change_the_snapshot(self) -> None:
        """Scenario 5: approve at 17:04, send at 21:30, data changed at 19:00.

        The bytes sent must still be the bytes approved.
        """
        tasks, snapshot = self.build()
        frozen_body = snapshot.rendered_body

        tasks[0] = apply_edit(tasks[0], {"title": "Renamed after approval"}, now=NOW)

        assert snapshot.rendered_body == frozen_body
        assert "Renamed after approval" not in snapshot.rendered_body

    def test_snapshot_records_the_summary_it_was_built_from(self) -> None:
        _, snapshot = self.build()
        assert snapshot.summary["completed"] == 1
        assert snapshot.summary["remaining"] == 2
