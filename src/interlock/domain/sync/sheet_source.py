"""Which Google Sheet the tasks come from, and how a task remembers it.

Pure: no I/O. Two ideas live here.

**A sheet link.** What people paste is a browser address
(``.../spreadsheets/d/<id>/edit#gid=0``). Only the spreadsheet id and the tab
(``gid``) matter; everything Interlock ever fetches is rebuilt from those two
values on ``docs.google.com``, so a pasted link can never make it request
anything but a Google Sheets export.

**A scope.** ``<id>:<gid>`` names one tab of one spreadsheet. Every imported
task's ``external_row_ref`` is ``<scope>|sr:<Sr No.>``, so a row can only ever
match the task that came from the *same* sheet. Without it, switching sheets
would match the new sheet's ``Sr No. 5`` to the old sheet's task 5: it would
overwrite that task, keep its history, and un-hide anything retired.

Refs written before scopes existed are plain ``sr:<n>`` ("legacy"); they belong
to whichever sheet was configured at the time, and are stamped with its scope
the first time the sheet is recorded (``SheetSourceService.current``).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re

_SPREADSHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_GID = re.compile(r"[#?&]gid=(\d+)")

MAX_LINK_LENGTH = 2000

RETIRED_TAG = "retired:sheet"
"""Marks a task hidden because its sheet is no longer the one in use. Distinct
from the delegation tag, so switching back restores exactly these tasks and
never one that is hidden because it was handed to someone else."""

_SR_PREFIX = "sr:"


class InvalidSheetLinkError(ValueError):
    """The text is not a Google Sheets link. The message is for the user."""


@dataclasses.dataclass(frozen=True, slots=True)
class SheetLink:
    spreadsheet_id: str
    gid: str = "0"

    @property
    def scope(self) -> str:
        return f"{self.spreadsheet_id}:{self.gid}"

    @property
    def export_url(self) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            f"/export?format=csv&gid={self.gid}"
        )


def parse_sheet_link(text: str) -> SheetLink:
    """The sheet a pasted link points at. No ``gid`` means the first tab."""
    link = text.strip()
    if not link:
        raise InvalidSheetLinkError("Paste the link to your Google Sheet.")
    if len(link) > MAX_LINK_LENGTH:
        raise InvalidSheetLinkError("That link is too long to be a Google Sheets link.")
    match = _SPREADSHEET_ID.search(link)
    if match is None:
        raise InvalidSheetLinkError(
            "That doesn't look like a Google Sheets link. Open your sheet in the browser "
            "and copy the address from the address bar (it contains /spreadsheets/d/)."
        )
    gid = _GID.search(link)
    return SheetLink(match.group(1), gid.group(1) if gid else "0")


def scoped_ref(scope: str, external_ref: str) -> str:
    """``<scope>|sr:5``, or just ``sr:5`` when there is no scope (legacy)."""
    return f"{scope}|{external_ref}" if scope else external_ref


def scope_of_ref(ref: str | None) -> str:
    """The sheet a task's ref belongs to; ``""`` for a legacy, unscoped ref."""
    if not ref or "|" not in ref:
        return ""
    return ref.rpartition("|")[0]


def sr_no_of_ref(ref: str | None) -> str | None:
    """The sheet's own ``Sr No.`` out of a ref, scoped or not."""
    if not ref:
        return None
    tail = ref.rpartition("|")[2]
    return tail.removeprefix(_SR_PREFIX) if tail.startswith(_SR_PREFIX) else None


@dataclasses.dataclass(frozen=True, slots=True)
class SheetSourceState:
    """The saved sheet setting and how the last read of it went."""

    url: str | None
    scope: str | None
    updated_at: dt.datetime
    updated_by: str
    import_pending: bool
    last_attempt_at: dt.datetime | None = None
    last_success_at: dt.datetime | None = None
    last_task_count: int | None = None
    last_error_code: str | None = None
    last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.scope)
