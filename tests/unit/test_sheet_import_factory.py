"""build_sheet_reader: the read-only import reads links, unless the two-way
sync owns the sheet."""

from __future__ import annotations

from interlock.adapters.sheets.factory import build_sheet_reader
from interlock.config import Settings, SheetsProviderName


def test_reads_links_by_default() -> None:
    assert callable(build_sheet_reader(Settings()))


def test_a_configured_link_changes_nothing_about_that() -> None:
    assert callable(build_sheet_reader(Settings(sheet_import_url="https://x/spreadsheets/d/abc/edit")))


def test_is_off_while_the_two_way_sync_owns_the_sheet() -> None:
    assert build_sheet_reader(Settings(sheets_provider=SheetsProviderName.GOOGLE)) is None
