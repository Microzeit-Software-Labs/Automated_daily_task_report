"""Builds the configured :class:`SpreadsheetProvider`.

The one place that decides mock vs. real, shared by both composition roots
(``api/main.py`` and ``workers/loop.py``) so they cannot pick different
providers for the same ``SHEETS_PROVIDER`` setting.
"""

from __future__ import annotations

from collections.abc import Callable

from interlock.adapters.sheets.csv_source import fetch_csv
from interlock.adapters.sheets.fake import FakeSpreadsheetProvider
from interlock.adapters.sheets.google_sheets import GoogleSheetsProvider
from interlock.config import Settings, SheetsProviderName
from interlock.domain.ports.sheets import SpreadsheetProvider
from interlock.domain.sync.sheet_source import SheetLink


def build_sheets_provider(settings: Settings) -> SpreadsheetProvider:
    if settings.sheets_provider is SheetsProviderName.MOCK:
        return FakeSpreadsheetProvider()
    if not settings.google_service_account_path or not settings.google_sheets_spreadsheet_id:
        raise RuntimeError(
            "SHEETS_PROVIDER=google requires GOOGLE_SERVICE_ACCOUNT_PATH and "
            "GOOGLE_SHEETS_SPREADSHEET_ID to be set -- see docs/google-sheets-setup.md."
        )
    return GoogleSheetsProvider(
        service_account_path=settings.google_service_account_path,
        spreadsheet_id=settings.google_sheets_spreadsheet_id,
        sheet_name=settings.google_sheets_sheet_name,
    )


def build_sheet_reader(settings: Settings) -> Callable[[SheetLink], str] | None:
    """What reads a sheet's CSV for the read-only import, or ``None`` when the
    two-way sync (``SHEETS_PROVIDER=google``) owns the sheet. The two would both
    create tasks from a sheet, and the two-way sync would then write the imported
    ones back out into its own tab, so they never coexist."""
    if settings.sheets_provider is SheetsProviderName.GOOGLE:
        return None
    return _read_link


def _read_link(link: SheetLink) -> str:
    return fetch_csv(link.export_url)
