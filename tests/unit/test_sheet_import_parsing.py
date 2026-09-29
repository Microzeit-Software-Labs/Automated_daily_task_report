"""Parsing a hand-kept task sheet. The fixture mirrors the real "Kaif Task
List" layout and its real quirks: trailing-space headers, mixed day-first /
month-first dates, a blank Sr No., delegated statuses, empty dropdown rows."""

from __future__ import annotations

import datetime as dt

import pytest

from interlock.domain.common.errors import ValidationFailedError
from interlock.domain.sync.sheet_import import (
    SkipReason,
    normalize_header,
    parse_sheet,
    parse_sheet_date,
    parse_status,
)
from interlock.domain.tasks.entities import TaskStatus

TODAY = dt.date(2026, 9, 29)

SHEET = """Sr No.,Date,Task,Status ,Note,Deadline
1,29-04-2026,ICCC Integration ,Closed,completed,
2,05-25-2026,Health-Check Up ,Closed,"Deployed, working",
,05-11-2026,Microplus update,Closed,when is the rest going on,
24,05-25-2026,Sri Lanka Issue(supervising),Subhan's Task,firewall issue,
77,09-17,Eldorado C2V extraction ,Inprogress,Waiting for the server ,
94,09-28-26,Extend Gold gym license,Inprogress,,10-05-2026
,,,,,
"""


class TestHeaders:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Sr No.", "sr no"),
            ("SR. NO", "sr no"),
            ("Status ", "status"),
            ("Deadline ", "deadline"),
        ],
    )
    def test_normalises_punctuation_case_and_spacing(
        self, raw: str, expected: str
    ) -> None:
        assert normalize_header(raw) == expected

    def test_a_missing_required_column_refuses_to_import_anything(self) -> None:
        with pytest.raises(ValidationFailedError, match="status"):
            parse_sheet("Sr No.,Date,Task\n1,01-01-2026,x\n", today=TODAY)

    def test_an_empty_sheet_is_an_error_not_zero_rows(self) -> None:
        with pytest.raises(ValidationFailedError):
            parse_sheet("", today=TODAY)


class TestDates:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("05-25-2026", dt.date(2026, 5, 25)),  # month first -- most rows
            ("29-04-2026", dt.date(2026, 4, 29)),  # day first -- the first rows
            ("05-11-2026", dt.date(2026, 5, 11)),  # ambiguous -> month first
            ("09-28-26", dt.date(2026, 9, 28)),  # two-digit year
            ("09-17", dt.date(2026, 9, 17)),  # no year -> default year
            ("2026-09-20", dt.date(2026, 9, 20)),  # ISO
            ("20/9/2026", dt.date(2026, 9, 20)),
        ],
    )
    def test_reads_every_format_the_sheet_uses(self, raw: str, expected: dt.date) -> None:
        assert parse_sheet_date(raw, default_year=2026) == expected

    @pytest.mark.parametrize("raw", ["", "  ", "tomorrow", "13-13-2026", "02-30-2026"])
    def test_unreadable_dates_are_none_never_a_guess(self, raw: str) -> None:
        assert parse_sheet_date(raw, default_year=2026) is None


class TestStatuses:
    @pytest.mark.parametrize(
        ("raw", "status"),
        [
            ("Closed", TaskStatus.COMPLETED),
            ("Inprogress", TaskStatus.IN_PROGRESS),
            ("In Progress", TaskStatus.IN_PROGRESS),
            ("", TaskStatus.PENDING),
        ],
    )
    def test_known_statuses(self, raw: str, status: TaskStatus) -> None:
        assert parse_status(raw) == (status, None)

    @pytest.mark.parametrize(
        ("raw", "name"),
        [
            ("Subhan's Task", "Subhan"),
            ("Subhan\u2019s Task", "Subhan"),  # phone-autocorrected apostrophe
            ("Safwan Task", "Safwan"),
            ("Safwaan Task", "Safwaan"),
            ("Abbas Task", "Abbas"),  # not "Abba" + possessive s
        ],
    )
    def test_delegated_statuses_name_the_person(self, raw: str, name: str) -> None:
        assert parse_status(raw) == (TaskStatus.IN_PROGRESS, name)

    def test_an_unknown_status_is_none(self) -> None:
        assert parse_status("waiting on vendor") == (None, None)


class TestParseSheet:
    def test_imports_keyed_rows_and_ignores_empty_ones(self) -> None:
        parsed = parse_sheet(SHEET, today=TODAY)
        assert [row.sr_no for row in parsed.rows] == ["1", "2", "24", "77", "94"]

    def test_row_fields_are_mapped_and_trimmed(self) -> None:
        row = next(r for r in parse_sheet(SHEET, today=TODAY).rows if r.sr_no == "94")
        assert row.title == "Extend Gold gym license"
        assert row.status is TaskStatus.IN_PROGRESS
        assert row.due_date == dt.date(2026, 10, 5)
        assert row.logged_on == dt.date(2026, 9, 28)
        assert row.external_ref == "sr:94"
        assert row.line == 7

    def test_quoted_commas_stay_inside_the_note(self) -> None:
        row = next(r for r in parse_sheet(SHEET, today=TODAY).rows if r.sr_no == "2")
        assert row.remarks == "Deployed, working"

    def test_a_delegated_row_is_kept_and_marked(self) -> None:
        row = next(r for r in parse_sheet(SHEET, today=TODAY).rows if r.sr_no == "24")
        assert row.delegated_to == "Subhan"

    def test_a_blank_sr_no_is_skipped_and_reported(self) -> None:
        skipped = parse_sheet(SHEET, today=TODAY).skipped
        assert [(s.line, s.reason) for s in skipped] == [(4, SkipReason.BLANK_SR_NO)]
        assert skipped[0].detail == "Microplus update"

    def test_a_blank_task_is_skipped_and_reported(self) -> None:
        parsed = parse_sheet("Sr No.,Task,Status\n5,,Closed\n", today=TODAY)
        assert parsed.rows == ()
        assert parsed.skipped[0].reason is SkipReason.BLANK_TASK

    def test_duplicate_sr_nos_skip_every_copy_rather_than_guess(self) -> None:
        parsed = parse_sheet("Sr No.,Task,Status\n5,a,Closed\n5,b,Closed\n6,c,\n", today=TODAY)
        assert [row.sr_no for row in parsed.rows] == ["6"]
        assert {s.reason for s in parsed.skipped} == {SkipReason.DUPLICATE_SR_NO}
        assert len(parsed.skipped) == 2

    def test_optional_columns_may_be_absent(self) -> None:
        parsed = parse_sheet("Sr No.,Task,Status\n1,Only the basics,Closed\n", today=TODAY)
        row = parsed.rows[0]
        assert row.remarks == ""
        assert row.due_date is None
        assert row.logged_on is None

    def test_an_overlong_title_is_truncated_to_the_task_limit(self) -> None:
        parsed = parse_sheet(f"Sr No.,Task,Status\n1,{'x' * 400},\n", today=TODAY)
        assert len(parsed.rows[0].title) == 300
