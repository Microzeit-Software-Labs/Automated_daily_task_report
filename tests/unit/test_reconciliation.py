"""The Sheets reconciliation truth table and the pure helpers around it.

This is the rule that decides whether a tick pushes, pulls, parks a
conflict, or does nothing -- getting it wrong means either silently
overwriting a human's spreadsheet edit or silently dropping an app edit.
"""

from __future__ import annotations

import datetime as dt

from interlock.domain.sync.reconciliation import (
    SyncAction,
    SyncState,
    canonicalize,
    content_hash,
    decide_action,
    editable_fields,
    resolve_baseline,
    task_content_fields,
)
from interlock.domain.tasks.entities import EDITABLE_FIELDS, Priority, TaskStatus
from tests.conftest import BRIEF_EVENING, make_task

HASH_A = "a" * 64
HASH_B = "b" * 64


def make_state(
    *,
    task_id: str = "task-1",
    last_synced_version: int = 1,
    last_synced_content_hash: str = HASH_A,
    last_synced_at: dt.datetime = BRIEF_EVENING,
) -> SyncState:
    return SyncState(
        task_id=task_id,
        last_synced_version=last_synced_version,
        last_synced_content_hash=last_synced_content_hash,
        last_synced_at=last_synced_at,
    )


class TestDecideAction:
    def test_neither_changed_is_noop(self) -> None:
        assert decide_action(db_changed=False, sheet_changed=False) is SyncAction.NOOP

    def test_db_only_is_push(self) -> None:
        assert decide_action(db_changed=True, sheet_changed=False) is SyncAction.PUSH

    def test_sheet_only_is_pull(self) -> None:
        assert decide_action(db_changed=False, sheet_changed=True) is SyncAction.PULL

    def test_both_changed_is_conflict(self) -> None:
        assert decide_action(db_changed=True, sheet_changed=True) is SyncAction.CONFLICT


class TestEditableFields:
    def test_keeps_exactly_the_nine_editable_fields(self) -> None:
        fields = dict.fromkeys(EDITABLE_FIELDS, "x") | {
            "id": "task-1",
            "version": 3,
            "created_at": "2026-09-15T00:00:00+00:00",
        }
        assert set(editable_fields(fields)) == set(EDITABLE_FIELDS)

    def test_drops_system_owned_fields(self) -> None:
        fields = editable_fields({"id": "task-1", "version": 3, "title": "Deploy"})
        assert "id" not in fields
        assert "version" not in fields
        assert fields["title"] == "Deploy"


class TestTaskContentFields:
    def test_shapes_enums_as_their_string_value(self) -> None:
        task = make_task(status=TaskStatus.IN_PROGRESS, priority=Priority.HIGH)
        fields = task_content_fields(task)
        assert fields["status"] == "IN_PROGRESS"
        assert fields["priority"] == "HIGH"

    def test_shapes_due_date_as_an_iso_string(self) -> None:
        task = make_task(due_date=dt.date(2026, 9, 20))
        assert task_content_fields(task)["due_date"] == "2026-09-20"

    def test_shapes_tags_as_a_list(self) -> None:
        task = make_task(tags=("urgent", "infra"))
        assert task_content_fields(task)["tags"] == ["urgent", "infra"]

    def test_omits_system_owned_fields(self) -> None:
        task = make_task()
        fields = task_content_fields(task)
        assert "id" not in fields
        assert "version" not in fields
        assert "created_at" not in fields


class TestContentHash:
    def test_deterministic_for_the_same_fields(self) -> None:
        fields = {"title": "Deploy", "tags": ["a", "b"]}
        assert content_hash(fields) == content_hash(dict(fields))

    def test_independent_of_key_order(self) -> None:
        assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})

    def test_different_values_hash_differently(self) -> None:
        assert content_hash({"title": "Deploy"}) != content_hash({"title": "Deploy "})


class TestCanonicalize:
    def test_strips_free_text_fields(self) -> None:
        fields = {"title": "  Deploy  ", "description": " notes ", "remarks": " fyi "}
        assert canonicalize(fields) == {
            "title": "Deploy",
            "description": "notes",
            "remarks": "fyi",
        }

    def test_leaves_other_fields_untouched(self) -> None:
        fields = {"project_id": " proj-1 ", "tags": ["a", "b"], "due_date": "2026-09-20"}
        assert canonicalize(fields) == fields

    def test_leaves_none_values_alone(self) -> None:
        assert canonicalize({"title": None}) == {"title": None}

    def test_idempotent(self) -> None:
        fields = {"title": "  Deploy  "}
        once = canonicalize(fields)
        assert canonicalize(once) == once

    def test_makes_a_whitespace_variant_hash_identically(self) -> None:
        """The whole point: without this, a title that round-tripped through
        a real sheet (which strips on read) would hash differently from the
        same title read straight off the task, forever."""
        with_whitespace = {"title": "Deploy production  "}
        stripped = {"title": "Deploy production"}
        assert content_hash(canonicalize(with_whitespace)) == content_hash(canonicalize(stripped))


class TestResolveBaseline:
    def test_cursor_wins_when_present(self) -> None:
        state = make_state(last_synced_version=5, last_synced_content_hash=HASH_A)
        baseline = resolve_baseline(state=state, stamped_version=99, stamped_content_hash=HASH_B)
        assert baseline is not None
        assert baseline.version == 5
        assert baseline.content_hash == HASH_A

    def test_falls_back_to_the_stamp_when_cursor_is_missing(self) -> None:
        baseline = resolve_baseline(
            state=None, stamped_version=3, stamped_content_hash=HASH_B
        )
        assert baseline is not None
        assert baseline.version == 3
        assert baseline.content_hash == HASH_B

    def test_none_when_neither_cursor_nor_stamp_exists(self) -> None:
        assert resolve_baseline(state=None, stamped_version=None, stamped_content_hash=None) is None

    def test_a_stamp_missing_its_hash_is_unusable(self) -> None:
        assert resolve_baseline(state=None, stamped_version=3, stamped_content_hash=None) is None

    def test_a_stamp_missing_its_version_is_unusable(self) -> None:
        baseline = resolve_baseline(state=None, stamped_version=None, stamped_content_hash=HASH_A)
        assert baseline is None
