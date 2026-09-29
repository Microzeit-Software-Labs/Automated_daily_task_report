"""Builds the configured :class:`SpreadsheetProvider`.

The one place that decides mock vs. real, shared by both composition roots
(``api/main.py`` and ``workers/loop.py``) so they cannot pick different
providers for the same ``SHEETS_PROVIDER`` setting.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

from interlock.adapters.sheets.csv_source import export_url_from_sheet_link, fetch_csv
from interlock.adapters.sheets.fake import FakeSpreadsheetProvider
from interlock.adapters.sheets.google_sheets import GoogleSheetsProvider
from interlock.config import Settings, SheetsProviderName
from interlock.domain.ports.sheets import SpreadsheetProvider


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


def build_sheet_import_fetch(settings: Settings) -> Callable[[], str] | None:
    """The read-only sheet import's fetch function, or ``None`` when
    ``SHEET_IMPORT_URL`` is unset. Refuses to coexist with the two-way sync:
    both would create tasks from a sheet, and the two-way sync would then
    write the imported ones back out into its own tab."""
    if not settings.sheet_import_url:
        return None
    if settings.sheets_provider is SheetsProviderName.GOOGLE:
        raise RuntimeError(
            "SHEET_IMPORT_URL and SHEETS_PROVIDER=google are mutually exclusive -- "
            "choose the read-only import or the two-way sync, not both. "
            "See docs/sheet-import-setup.md."
        )
    return functools.partial(fetch_csv, export_url_from_sheet_link(settings.sheet_import_url))
