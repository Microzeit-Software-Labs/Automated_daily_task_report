"""build_sheet_import_fetch: off unless configured, and never alongside the
two-way sync."""

from __future__ import annotations

import pytest

from interlock.adapters.sheets.factory import build_sheet_import_fetch
from interlock.config import Settings, SheetsProviderName

LINK = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0"


def test_off_by_default() -> None:
    assert build_sheet_import_fetch(Settings(sheet_import_url=None)) is None


def test_on_when_a_link_is_set() -> None:
    assert callable(build_sheet_import_fetch(Settings(sheet_import_url=LINK)))


def test_refuses_to_run_alongside_the_two_way_sync() -> None:
    settings = Settings(sheet_import_url=LINK, sheets_provider=SheetsProviderName.GOOGLE)
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        build_sheet_import_fetch(settings)


def test_a_bad_link_fails_at_startup_not_at_the_first_tick() -> None:
    with pytest.raises(ValueError, match="Google Sheets link"):
        build_sheet_import_fetch(Settings(sheet_import_url="https://example.com"))
