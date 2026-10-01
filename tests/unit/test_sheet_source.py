"""Sheet links, scopes and the plain-language reasons a sheet can't be used."""

from __future__ import annotations

import urllib.error
from email.message import Message

import pytest

from interlock.adapters.sheets.csv_source import SheetNotReadableError
from interlock.domain.common.errors import ValidationFailedError
from interlock.domain.sync.sheet_source import (
    InvalidSheetLinkError,
    SheetLink,
    parse_sheet_link,
    scope_of_ref,
    scoped_ref,
    sr_no_of_ref,
)
from interlock.services.sheet_source_service import classify_problem

pytestmark = pytest.mark.unit


class TestParseSheetLink:
    def test_a_normal_browser_link(self) -> None:
        link = parse_sheet_link("https://docs.google.com/spreadsheets/d/1AbC_-xyz/edit#gid=123")

        assert link == SheetLink("1AbC_-xyz", "123")
        assert link.scope == "1AbC_-xyz:123"

    def test_no_gid_means_the_first_tab(self) -> None:
        assert parse_sheet_link("https://docs.google.com/spreadsheets/d/abc/edit").gid == "0"

    def test_gid_in_the_query_string(self) -> None:
        assert parse_sheet_link("https://docs.google.com/spreadsheets/d/abc/edit?gid=7").gid == "7"

    def test_surrounding_whitespace_from_a_paste_is_ignored(self) -> None:
        pasted = "  https://docs.google.com/spreadsheets/d/abc/edit\n"

        assert parse_sheet_link(pasted).spreadsheet_id == "abc"

    def test_the_export_url_is_rebuilt_on_google_whatever_was_pasted(self) -> None:
        """A pasted link can never make Interlock fetch anything but a Google
        Sheets export: only the id and tab survive."""
        link = parse_sheet_link("https://evil.example/spreadsheets/d/abc123/edit#gid=5?x=http://169.254.169.254/")

        assert link.export_url == (
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=5"
        )

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "https://example.com/whatever", "my tasks", "https://docs.google.com/document/d/abc/edit"],
    )
    def test_something_that_is_not_a_sheet_link_is_refused_with_advice(self, text: str) -> None:
        with pytest.raises(InvalidSheetLinkError) as caught:
            parse_sheet_link(text)

        assert str(caught.value)  # something a person can act on

    def test_an_absurdly_long_link_is_refused(self) -> None:
        with pytest.raises(InvalidSheetLinkError, match="too long"):
            parse_sheet_link("https://docs.google.com/spreadsheets/d/abc/edit#" + "x" * 3000)


class TestRefs:
    def test_a_ref_is_scoped_to_its_sheet(self) -> None:
        assert scoped_ref("abc:0", "sr:5") == "abc:0|sr:5"

    def test_no_scope_leaves_the_ref_alone(self) -> None:
        assert scoped_ref("", "sr:5") == "sr:5"

    def test_scope_of_a_ref(self) -> None:
        assert scope_of_ref("abc:0|sr:5") == "abc:0"
        assert scope_of_ref("sr:5") == ""  # legacy: written before scopes existed
        assert scope_of_ref(None) == ""

    def test_the_sheets_own_number_from_either_kind_of_ref(self) -> None:
        assert sr_no_of_ref("abc:0|sr:12") == "12"
        assert sr_no_of_ref("sr:12") == "12"
        assert sr_no_of_ref(None) is None
        assert sr_no_of_ref("something-else") is None

    def test_the_same_sr_no_in_two_sheets_is_two_refs(self) -> None:
        assert scoped_ref("a:0", "sr:1") != scoped_ref("b:0", "sr:1")


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "x", Message(), None)


class TestClassifyProblem:
    def test_not_a_link(self) -> None:
        assert classify_problem(InvalidSheetLinkError("nope")).code == "NOT_A_SHEET_LINK"

    def test_a_sheet_that_is_not_shared_says_how_to_share_it(self) -> None:
        for error in (SheetNotReadableError("html sign-in page"), _http(403), _http(401)):
            problem = classify_problem(error)

            assert problem.code == "NOT_SHARED"
            assert "Anyone with the link" in problem.message

    def test_a_missing_sheet(self) -> None:
        assert classify_problem(_http(404)).code == "NOT_FOUND"

    def test_google_having_a_bad_day(self) -> None:
        assert classify_problem(_http(503)).code == "UNREACHABLE"

    def test_no_internet(self) -> None:
        assert classify_problem(urllib.error.URLError("no route")).code == "UNREACHABLE"
        assert classify_problem(TimeoutError()).code == "UNREACHABLE"

    def test_missing_columns_are_named_in_the_sheets_own_words(self) -> None:
        error = ValidationFailedError("x", missing=["sr no", "task"], found=["date", "note"])

        problem = classify_problem(error)

        assert problem.code == "WRONG_COLUMNS"
        assert "Sr No., Task" in problem.message
        assert "date, note" in problem.message

    def test_a_completely_empty_sheet(self) -> None:
        assert classify_problem(ValidationFailedError("The sheet is empty")).code == "EMPTY"

    def test_anything_else_is_still_an_answer(self) -> None:
        problem = classify_problem(RuntimeError("boom"))

        assert problem.code == "UNEXPECTED"
        assert "boom" in problem.message
