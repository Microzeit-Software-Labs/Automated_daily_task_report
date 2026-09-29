"""Parsing a hand-kept task sheet into rows Interlock can mirror.

Pure: no network, no database. The sheet is edited by a person, not a
program, so every rule here leans towards "skip and report" over "guess" --
a mis-imported task ends up in a report sent to a manager, which is far
worse than a row the owner is told was skipped.

The expected columns (matched by header text, case- and punctuation-
insensitive, so ``"Status "`` and ``"Sr No."`` both match): ``Sr No``,
``Task`` and ``Status`` are required; ``Date``, ``Note`` and ``Deadline`` are
optional.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import re
from collections import Counter
from enum import StrEnum

from interlock.domain.common.errors import ValidationFailedError
from interlock.domain.tasks.entities import MAX_TITLE_LENGTH, TaskStatus

SR_NO = "sr no"
DATE = "date"
TASK = "task"
STATUS = "status"
NOTE = "note"
DEADLINE = "deadline"
REQUIRED_HEADERS = (SR_NO, TASK, STATUS)

_STATUS_MAP: dict[str, TaskStatus] = {
    "": TaskStatus.PENDING,
    "pending": TaskStatus.PENDING,
    "open": TaskStatus.PENDING,
    "todo": TaskStatus.PENDING,
    "inprogress": TaskStatus.IN_PROGRESS,
    "ongoing": TaskStatus.IN_PROGRESS,
    "closed": TaskStatus.COMPLETED,
    "done": TaskStatus.COMPLETED,
    "completed": TaskStatus.COMPLETED,
    "complete": TaskStatus.COMPLETED,
    "blocked": TaskStatus.BLOCKED,
    "deferred": TaskStatus.DEFERRED,
    "onhold": TaskStatus.DEFERRED,
}

# "Subhan's Task", "Safwan Task". U+2019 is the curly apostrophe phones
# autocorrect to. No bare-"s" alternative in the possessive: it would read
# "Abbas Task" as belonging to "Abba".
_DELEGATED = re.compile(r"^(?P<name>.+?)(?:'s|\u2019s)?\s+task$", re.IGNORECASE)
_DMY_OR_MDY = re.compile(r"^(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{4}|\d{2}))?$")


class SkipReason(StrEnum):
    BLANK_SR_NO = "BLANK_SR_NO"
    DUPLICATE_SR_NO = "DUPLICATE_SR_NO"
    BLANK_TASK = "BLANK_TASK"


@dataclasses.dataclass(frozen=True, slots=True)
class SkippedRow:
    line: int
    """The sheet's own row number (the header is row 1)."""
    reason: SkipReason
    detail: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class ImportedRow:
    line: int
    sr_no: str
    title: str
    status: TaskStatus | None
    """``None`` means the status text was not recognised; the importer keeps
    an existing task's status rather than guessing a new one."""
    raw_status: str
    delegated_to: str | None
    remarks: str
    due_date: dt.date | None
    logged_on: dt.date | None

    @property
    def external_ref(self) -> str:
        return f"sr:{self.sr_no}"


@dataclasses.dataclass(frozen=True, slots=True)
class ParsedSheet:
    rows: tuple[ImportedRow, ...]
    skipped: tuple[SkippedRow, ...]


def normalize_header(raw: str) -> str:
    """``"Sr No."`` / ``"SR. NO"`` / ``"Status "`` -> ``"sr no"`` / ``"status"``."""
    return re.sub(r"[\s.]+", " ", raw.strip().casefold()).strip()


def parse_sheet_date(raw: str, *, default_year: int) -> dt.date | None:
    """Month-first unless the first number cannot be a month, in which case
    day-first -- this sheet mixes ``29-04-2026`` and ``05-25-2026``. A date
    that is genuinely ambiguous (``05-11-2026``) is read month-first, the
    format most rows use. Two-digit years mean 20xx; a missing year means
    ``default_year``. Anything else is ``None``, never a guess."""
    text = raw.strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        pass

    match = _DMY_OR_MDY.match(text)
    if match is None:
        return None
    first, second = int(match.group(1)), int(match.group(2))
    year_text = match.group(3)
    if year_text is None:
        year = default_year
    else:
        year = int(year_text) + (2000 if len(year_text) == 2 else 0)
    month, day = (first, second) if first <= 12 else (second, first)
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_status(raw: str) -> tuple[TaskStatus | None, str | None]:
    """``(status, delegated_to)``. A delegated row is IN_PROGRESS -- someone
    is working on it, just not the sheet's owner -- and the importer hides it
    from reports entirely."""
    text = raw.strip()
    key = re.sub(r"[\s_-]+", "", text.casefold())
    if key in _STATUS_MAP:
        return _STATUS_MAP[key], None
    delegated = _DELEGATED.match(text)
    if delegated is not None:
        return TaskStatus.IN_PROGRESS, delegated.group("name").strip()
    return None, None


def parse_sheet(csv_text: str, *, today: dt.date) -> ParsedSheet:
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        raise ValidationFailedError("The sheet is empty -- not even a header row.")

    columns: dict[str, int] = {}
    for index, raw in enumerate(rows[0]):
        columns.setdefault(normalize_header(raw), index)
    missing = [header for header in REQUIRED_HEADERS if header not in columns]
    if missing:
        raise ValidationFailedError(
            "The sheet is missing required column(s): "
            f"{', '.join(missing)}. Nothing was imported.",
            missing=missing,
            found=[normalize_header(h) for h in rows[0]],
        )

    def cell(row: list[str], name: str) -> str:
        index = columns.get(name)
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    candidates: list[ImportedRow] = []
    skipped: list[SkippedRow] = []
    for offset, row in enumerate(rows[1:], start=2):
        sr_no = cell(row, SR_NO)
        title = cell(row, TASK)
        if not sr_no and not title:
            continue  # an empty row, or one holding only a dropdown default
        if not sr_no:
            skipped.append(SkippedRow(offset, SkipReason.BLANK_SR_NO, title))
            continue
        if not title:
            skipped.append(SkippedRow(offset, SkipReason.BLANK_TASK, f"Sr No. {sr_no}"))
            continue

        raw_status = cell(row, STATUS)
        status, delegated_to = parse_status(raw_status)
        candidates.append(
            ImportedRow(
                line=offset,
                sr_no=sr_no,
                title=title[:MAX_TITLE_LENGTH],
                status=status,
                raw_status=raw_status,
                delegated_to=delegated_to,
                remarks=cell(row, NOTE),
                due_date=parse_sheet_date(cell(row, DEADLINE), default_year=today.year),
                logged_on=parse_sheet_date(cell(row, DATE), default_year=today.year),
            )
        )

    counts = Counter(row.sr_no for row in candidates)
    kept: list[ImportedRow] = []
    for candidate in candidates:
        if counts[candidate.sr_no] > 1:
            skipped.append(
                SkippedRow(candidate.line, SkipReason.DUPLICATE_SR_NO, f"Sr No. {candidate.sr_no}")
            )
        else:
            kept.append(candidate)

    return ParsedSheet(rows=tuple(kept), skipped=tuple(sorted(skipped, key=lambda s: s.line)))
