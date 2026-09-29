"""The sheet's column layout, and translating raw cell values to and from it.

Pure string/list wrangling -- no network, no Google client -- so it can be
unit-tested against plain fixtures and reused unchanged by both
:class:`~interlock.adapters.sheets.google_sheets.GoogleSheetsProvider` and
:class:`~interlock.adapters.sheets.fake.FakeSpreadsheetProvider`.

Reads look each column up by its header text, but writes are positional
(:func:`~interlock.adapters.sheets.google_sheets._row_values` emits
``HEADER_ROW`` order starting at column A), so the sheet's A..K layout must
match ``HEADER_ROW`` exactly -- a reordered column would have values written
into the wrong cells. See ``docs/google-sheets-setup.md`` for the exact header
row a new sheet needs.
"""

from __future__ import annotations

from typing import Any

SYS_COLUMN = "_sys"
DISPLAY_ID_COLUMN = "display_id"
"""Read-only in the sheet -- shown for human reference, never read back on
pull. A person renaming or blanking it has no effect on reconciliation."""

# Exactly interlock.domain.tasks.entities.EDITABLE_FIELDS, in the order a new
# sheet's header row should use.
FIELD_COLUMNS: tuple[str, ...] = (
    "title",
    "description",
    "project_id",
    "owner_id",
    "priority",
    "status",
    "due_date",
    "remarks",
    "tags",
)

HEADER_ROW: tuple[str, ...] = (SYS_COLUMN, DISPLAY_ID_COLUMN, *FIELD_COLUMNS)

_SYS_SEP = "|"
_TAGS_SEP = ","


def format_sys(*, task_id: str, version: int, content_hash: str) -> str:
    return _SYS_SEP.join((task_id, str(version), content_hash))


def parse_sys(raw: str) -> tuple[str | None, int | None, str | None]:
    """``"" -> (None, None, None)``; a malformed (non-empty, wrong shape)
    value also degrades to ``(None, None, None)`` -- treated the same as a
    brand new human-authored row rather than raising, since a stray edit to
    a cell that is not actually protected must not crash the sync tick."""
    if not raw:
        return None, None, None
    parts = raw.split(_SYS_SEP)
    if len(parts) != 3 or not parts[0] or not parts[1].isdigit():
        return None, None, None
    task_id, version_str, content_hash = parts
    return task_id, int(version_str), content_hash or None


def format_field_value(name: str, value: Any) -> str:
    """One task field's value, as the text that belongs in its cell."""
    if value is None:
        return ""
    if name == "tags":
        return _TAGS_SEP.join(value)
    return str(value)


def parse_field_value(name: str, raw: str) -> Any:
    """Inverse of :func:`format_field_value`, for the fields a sheet edit is
    allowed to change. Returns a JSON-shaped value -- the same shape
    :func:`interlock.domain.sync.reconciliation.task_content_fields`
    produces -- not yet a domain enum or date; the service layer does that
    conversion (and can reject invalid values without failing the row)."""
    if name == "tags":
        return [tag.strip() for tag in raw.split(_TAGS_SEP) if tag.strip()]
    if name in {"project_id", "owner_id", "due_date"}:
        return raw.strip() or None
    return raw.strip()
