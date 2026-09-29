"""The real Google Sheets v4 adapter.

One read call and at most two write calls per tick (a ``batchUpdate`` for
existing rows, an ``append`` for brand new ones) -- never a call per row.
Deliberately skips the Drive ``changes`` feed the architecture doc mentions
as an optimization: for a single-user, daily-cadence sheet, reading the
whole range every tick is the doc's own documented fallback and is cheap
enough not to need it (see the "Honest limitation" callout in the
architecture artifact, §3). That also means this adapter only needs the
Sheets API scope, not Drive.

See ``docs/google-sheets-setup.md`` for how to create the service account
and share a spreadsheet with it.
"""

from __future__ import annotations

from collections.abc import Sequence

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from interlock.adapters.sheets.columns import (
    FIELD_COLUMNS,
    SYS_COLUMN,
    format_field_value,
    format_sys,
    parse_field_value,
    parse_sys,
)
from interlock.domain.ports.sheets import SheetRow, SheetRowWrite

_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


class GoogleSheetsProvider:
    def __init__(
        self, *, service_account_path: str, spreadsheet_id: str, sheet_name: str
    ) -> None:
        # google-auth ships from_service_account_file without a typed
        # signature, so this boundary call needs an explicit ignore -- same
        # treatment api/main.py gives add_middleware's own untyped edge.
        credentials = Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            service_account_path, scopes=_SCOPES
        )
        self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = sheet_name

    def read_all_rows(self) -> Sequence[SheetRow]:
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=f"{self._sheet_name}!A:Z")
            .execute()
        )
        values: list[list[str]] = result.get("values", [])
        if not values:
            return []

        # Reads are by header name, but writes (_row_values) are positional
        # from column A -- so this tolerates a reordered sheet on read only.
        # The layout must still match columns.HEADER_ROW for writes to land
        # in the right cells.
        column_index = {name: index for index, name in enumerate(values[0])}

        rows: list[SheetRow] = []
        for offset, raw_row in enumerate(values[1:], start=2):
            sys_task_id, sys_version, sys_hash = parse_sys(
                _cell(raw_row, column_index.get(SYS_COLUMN))
            )
            fields = {
                name: parse_field_value(name, _cell(raw_row, column_index.get(name)))
                for name in FIELD_COLUMNS
                if name in column_index
            }
            rows.append(
                SheetRow(
                    row_index=offset,
                    sys_task_id=sys_task_id,
                    sys_version=sys_version,
                    sys_content_hash=sys_hash,
                    fields=fields,
                )
            )
        return rows

    def write_rows(self, writes: Sequence[SheetRowWrite]) -> None:
        if not writes:
            return

        updates = [write for write in writes if write.row_index is not None]
        appends = [write for write in writes if write.row_index is None]

        if updates:
            data = [
                {"range": f"{self._sheet_name}!A{write.row_index}", "values": [_row_values(write)]}
                for write in updates
            ]
            self._service.spreadsheets().values().batchUpdate(
                spreadsheetId=self._spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()

        if appends:
            self._service.spreadsheets().values().append(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [_row_values(write) for write in appends]},
            ).execute()


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def _row_values(write: SheetRowWrite) -> list[str]:
    sys_value = format_sys(
        task_id=write.sys_task_id, version=write.sys_version, content_hash=write.sys_content_hash
    )
    values = [sys_value, write.display_id]
    values.extend(format_field_value(name, write.fields.get(name)) for name in FIELD_COLUMNS)
    return values
