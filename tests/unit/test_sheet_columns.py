"""The sheet's column layout and cell (de)serialization.

Pure string wrangling, so it is tested directly rather than only through
``FakeSpreadsheetProvider`` -- the fake stores ``fields`` dicts verbatim and
never goes through ``format_field_value``/``parse_field_value``, so it can
never catch a real round-trip asymmetry the way these tests do.
"""

from __future__ import annotations

import datetime as dt

from interlock.adapters.sheets.columns import (
    FIELD_COLUMNS,
    HEADER_ROW,
    SYS_COLUMN,
    format_field_value,
    format_sys,
    parse_field_value,
    parse_sys,
)
from interlock.domain.sync.reconciliation import canonicalize, task_content_fields
from interlock.domain.tasks.entities import EDITABLE_FIELDS, Priority, TaskStatus
from tests.conftest import make_task


class TestHeaderRow:
    def test_field_columns_matches_editable_fields_exactly(self) -> None:
        """Guards against the sheet layout and the domain's editable-field
        set drifting apart -- a column here with no matching domain field
        (or vice versa) would silently stop being synced."""
        assert set(FIELD_COLUMNS) == EDITABLE_FIELDS

    def test_header_row_starts_with_sys_then_display_id(self) -> None:
        assert HEADER_ROW[0] == SYS_COLUMN
        assert HEADER_ROW[1] == "display_id"
        assert HEADER_ROW[2:] == FIELD_COLUMNS


class TestSysStamp:
    def test_round_trips(self) -> None:
        raw = format_sys(task_id="task-1", version=3, content_hash="a" * 64)
        assert parse_sys(raw) == ("task-1", 3, "a" * 64)

    def test_empty_cell_is_no_stamp(self) -> None:
        assert parse_sys("") == (None, None, None)

    def test_malformed_stamp_degrades_to_no_stamp_rather_than_raising(self) -> None:
        assert parse_sys("not-a-real-stamp") == (None, None, None)
        assert parse_sys("task-1|not-a-number|hash") == (None, None, None)
        assert parse_sys("task-1|3") == (None, None, None)

    def test_a_stray_pipe_in_the_hash_position_does_not_crash(self) -> None:
        assert parse_sys("task-1|3|hash|extra") == (None, None, None)


class TestFieldValueRoundTrip:
    def test_tags_round_trip(self) -> None:
        raw = format_field_value("tags", ["urgent", "infra"])
        assert parse_field_value("tags", raw) == ["urgent", "infra"]

    def test_empty_tags_round_trip_to_an_empty_list(self) -> None:
        assert parse_field_value("tags", format_field_value("tags", [])) == []

    def test_nullable_field_round_trips_none_to_none(self) -> None:
        for name in ("project_id", "owner_id", "due_date"):
            assert parse_field_value(name, format_field_value(name, None)) is None

    def test_a_whitespace_only_nullable_cell_parses_to_none(self) -> None:
        assert parse_field_value("project_id", "   ") is None


class TestFreeTextRoundTripSymmetry:
    """The load-bearing case: ``parse_field_value`` strips on every read, so
    a task field with incidental surrounding whitespace must not permanently
    disagree with its own round-tripped value once ``canonicalize`` (see
    ``domain/sync/reconciliation.py``) has been applied to both sides."""

    def test_title_with_surrounding_whitespace_matches_after_canonicalize(self) -> None:
        task = make_task(title="  Deploy production server  ")
        fields = canonicalize(task_content_fields(task))

        written = format_field_value("title", fields["title"])
        read_back = parse_field_value("title", written)

        assert read_back == fields["title"]

    def test_description_and_remarks_also_round_trip_after_canonicalize(self) -> None:
        task = make_task(description="  needs review  ", remarks=" fyi ")
        fields = canonicalize(task_content_fields(task))

        for name in ("description", "remarks"):
            written = format_field_value(name, fields[name])
            assert parse_field_value(name, written) == fields[name]

    def test_a_clean_task_round_trips_every_field_unchanged(self) -> None:
        task = make_task(
            title="Deploy production server",
            description="",
            project_id=None,
            owner_id=None,
            due_date=dt.date(2026, 9, 20),
            remarks="",
            tags=("urgent",),
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.HIGH,
        )
        fields = canonicalize(task_content_fields(task))

        for name, value in fields.items():
            written = format_field_value(name, value)
            assert parse_field_value(name, written) == value
