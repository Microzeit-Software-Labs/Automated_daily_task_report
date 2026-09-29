"""The sheet CSV fetch: link -> export URL, and never mistaking a sheet
that went private (Google's 200 HTML sign-in page) for an empty sheet."""

from __future__ import annotations

from typing import Any

import pytest

from interlock.adapters.sheets import csv_source
from interlock.adapters.sheets.csv_source import (
    SheetNotReadableError,
    export_url_from_sheet_link,
    fetch_csv,
)

SHEET_ID = "1dCIwGwZbLc3G2ziy9tWok0qfLKVN4CzQU_xE7k9SSig"


class TestExportUrl:
    @pytest.mark.parametrize(
        ("link", "gid"),
        [
            (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?usp=sharing", "0"),
            (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0", "0"),
            (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid=123#gid=123", "123"),
        ],
    )
    def test_builds_the_csv_export_url(self, link: str, gid: str) -> None:
        assert export_url_from_sheet_link(link) == (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
        )

    def test_rejects_something_that_is_not_a_sheets_link(self) -> None:
        with pytest.raises(ValueError, match="Google Sheets link"):
            export_url_from_sheet_link("https://example.com/whatever")


class _FakeResponse:
    def __init__(self, body: str, content_type: str) -> None:
        self._body = body.encode("utf-8")
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _serve(monkeypatch: pytest.MonkeyPatch, body: str, content_type: str) -> None:
    def fake_urlopen(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(body, content_type)

    monkeypatch.setattr(csv_source.urllib.request, "urlopen", fake_urlopen)


class TestFetch:
    def test_returns_csv_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _serve(monkeypatch, "Sr No.,Task\n1,x\n", "text/csv; charset=utf-8")
        assert fetch_csv("https://example").startswith("Sr No.")

    def test_a_private_sheets_html_sign_in_page_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve(monkeypatch, "<!doctype html><html>Sign in</html>", "text/html; charset=utf-8")
        with pytest.raises(SheetNotReadableError, match="anyone with the link"):
            fetch_csv("https://example")

    def test_html_is_rejected_even_if_mislabelled_as_csv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve(monkeypatch, "  <html>nope</html>", "text/csv")
        with pytest.raises(SheetNotReadableError):
            fetch_csv("https://example")

    def test_a_utf8_bom_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _serve(monkeypatch, "\ufeffSr No.,Task\n", "text/csv")
        assert fetch_csv("https://example").startswith("Sr No.")

