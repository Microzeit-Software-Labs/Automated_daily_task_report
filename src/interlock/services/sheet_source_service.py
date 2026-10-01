"""Choosing which Google Sheet the tasks come from -- the way a phone is chosen
for WhatsApp: look before you switch, and a failed attempt changes nothing.

* ``check`` reads a pasted link *without saving anything* and says what it found
  (how many tasks, a few titles, how many current tasks would be hidden) or, if
  it can't use the sheet, why, in words a person can act on.
* ``apply`` re-checks (never trusts the browser's earlier check), then saves the
  link and asks the worker to import at once. The sheet already in use stays
  untouched until the new one has been read successfully.
* ``current`` is the saved setting. The first time it is asked for, an
  ``SHEET_IMPORT_URL`` still in ``.env`` seeds it; from then on the database
  wins, so a link changed in the browser can't be undone by an old ``.env``.

The importer itself (``SheetImportService``) does the reading and the retiring
of the old sheet's tasks when the worker next ticks.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import urllib.error
from collections.abc import Callable
from zoneinfo import ZoneInfo

from interlock.adapters.persistence.sheet_source_repository import SheetSourceRepository
from interlock.adapters.sheets.csv_source import SheetNotReadableError
from interlock.domain.common.actor import Actor, ActorKind, sync_actor
from interlock.domain.common.clock import local_date
from interlock.domain.common.errors import ValidationFailedError
from interlock.domain.ports.repositories import AuditSink, TaskFilter, TaskRepository
from interlock.domain.sync.sheet_import import parse_sheet
from interlock.domain.sync.sheet_source import (
    InvalidSheetLinkError,
    SheetLink,
    SheetSourceState,
    parse_sheet_link,
    scope_of_ref,
)
from interlock.domain.tasks.entities import TaskSourceKind

SAMPLE_TITLES = 3
ENTITY_ID = "sheet_source"

_HEADER_NAMES = {"sr no": "Sr No.", "task": "Task", "status": "Status"}

SHARE_HELP = (
    "Open the sheet, press Share, set General access to \"Anyone with the link\" with the "
    "Viewer role, then check again."
)


@dataclasses.dataclass(frozen=True, slots=True)
class SheetProblem:
    """Why a sheet can't be used. ``message`` is for the user."""

    code: str
    message: str


@dataclasses.dataclass(frozen=True, slots=True)
class SheetCheck:
    ok: bool
    link: SheetLink | None = None
    problem: SheetProblem | None = None
    task_count: int = 0
    sample_titles: tuple[str, ...] = ()
    skipped: int = 0
    will_hide: int = 0
    """Tasks from the sheet in use that this switch would hide from reports."""
    same_sheet: bool = False


def classify_problem(error: BaseException) -> SheetProblem:
    """A failure to read or understand a sheet, as something a person can act on."""
    if isinstance(error, InvalidSheetLinkError):
        return SheetProblem("NOT_A_SHEET_LINK", str(error))
    if isinstance(error, SheetNotReadableError):
        return SheetProblem(
            "NOT_SHARED", f"Google wouldn't let Interlock read this sheet. {SHARE_HELP}"
        )
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (401, 403):
            return SheetProblem(
                "NOT_SHARED", f"Google wouldn't let Interlock read this sheet. {SHARE_HELP}"
            )
        if error.code == 404:
            return SheetProblem(
                "NOT_FOUND",
                "Google says there is no sheet at that link. Check that the whole link was "
                "copied, and that the sheet hasn't been deleted.",
            )
        return SheetProblem(
            "UNREACHABLE", f"Google answered with an error (HTTP {error.code}). Try again shortly."
        )
    if isinstance(error, urllib.error.URLError | TimeoutError | OSError):
        return SheetProblem(
            "UNREACHABLE",
            "Interlock couldn't reach Google. Check the internet connection and try again.",
        )
    if isinstance(error, ValidationFailedError):
        if error.details.get("missing"):
            missing = ", ".join(_HEADER_NAMES.get(h, h) for h in error.details["missing"])
            found = ", ".join(error.details.get("found") or []) or "nothing"
            return SheetProblem(
                "WRONG_COLUMNS",
                f"This sheet is missing the column(s) Interlock needs: {missing}. "
                f"Its first row has: {found}.",
            )
        return SheetProblem("EMPTY", "The sheet is empty: it has no header row yet.")
    return SheetProblem("UNEXPECTED", f"Something unexpected went wrong: {error}")


class SheetSourceService:
    def __init__(
        self,
        *,
        repo: SheetSourceRepository,
        task_repo: TaskRepository,
        audit: AuditSink,
        tz: ZoneInfo,
        reader: Callable[[SheetLink], str] | None,
        seed_url: str | None = None,
    ) -> None:
        """``reader`` fetches a sheet's CSV; ``None`` means changing the sheet is
        not allowed here (the two-way sync owns the sheet)."""
        self._repo = repo
        self._tasks = task_repo
        self._audit = audit
        self._tz = tz
        self._reader = reader
        self._seed_url = seed_url

    @property
    def changeable(self) -> bool:
        return self._reader is not None

    # -- the saved setting --------------------------------------------------

    def current(self, *, now: dt.datetime) -> SheetSourceState | None:
        state = self._repo.get()
        if state is not None or not self._seed_url:
            return state
        try:
            link = parse_sheet_link(self._seed_url)
        except InvalidSheetLinkError:
            return None  # a bad .env value seeds nothing; the page says "not connected"
        if self._repo.create_if_absent(
            url=self._seed_url.strip(), scope=link.scope, by="env", now=now
        ):
            stamped = self._repo.stamp_legacy_refs(link.scope)
            self._audit.record(
                action="SHEET_SOURCE_SEEDED",
                entity_type="sheet_source",
                entity_id=ENTITY_ID,
                actor=str(sync_actor("sheet_source")),
                actor_kind=ActorKind.SYNC.value,
                at=now,
                after={
                    "url": self._seed_url.strip(),
                    "scope": link.scope,
                    "tasks_stamped": stamped,
                },
            )
        return self._repo.get()

    # -- look before you switch ---------------------------------------------

    def check(self, url: str, *, now: dt.datetime) -> SheetCheck:
        """Read the sheet at ``url`` and report. Saves nothing."""
        try:
            link = parse_sheet_link(url)
        except InvalidSheetLinkError as error:
            return SheetCheck(False, problem=classify_problem(error))
        if self._reader is None:
            return SheetCheck(
                False,
                link,
                SheetProblem(
                    "TWO_WAY_SYNC",
                    "The two-way Google Sheets sync is turned on, and it manages the sheet. "
                    "Turn it off (SHEETS_PROVIDER) to read tasks from a link instead.",
                ),
            )
        try:
            parsed = parse_sheet(self._reader(link), today=local_date(now, self._tz))
        except Exception as error:  # every failure becomes a plain-language answer
            return SheetCheck(False, link, classify_problem(error))
        if not parsed.rows:
            return SheetCheck(
                False,
                link,
                SheetProblem(
                    "EMPTY",
                    "The sheet has its header row but no tasks yet (each task needs a Sr No. "
                    "and a Task).",
                ),
                skipped=len(parsed.skipped),
            )

        state = self._repo.get()
        same = state is not None and state.scope == link.scope
        will_hide = 0
        if not same:
            will_hide = sum(
                1
                for task in self._tasks.list(TaskFilter())
                if task.source is TaskSourceKind.SHEET_IMPORT
                and scope_of_ref(task.external_row_ref) != link.scope
            )
        return SheetCheck(
            True,
            link,
            task_count=len(parsed.rows),
            sample_titles=tuple(row.title for row in parsed.rows[-SAMPLE_TITLES:]),
            skipped=len(parsed.skipped),
            will_hide=will_hide,
            same_sheet=same,
        )

    # -- switch ---------------------------------------------------------------

    def apply(self, url: str, *, actor: Actor, now: dt.datetime) -> SheetSourceState:
        """Use the sheet at ``url``. Raises ``ValidationFailedError`` (with the
        problem's ``code``) when it can't be read: nothing is saved then."""
        check = self.check(url, now=now)
        if not check.ok or check.link is None:
            problem = check.problem or SheetProblem("UNEXPECTED", "The sheet could not be used.")
            raise ValidationFailedError(problem.message, code=problem.code)

        before = self.current(now=now)
        # For the sheet already in use this just remembers the link as pasted and
        # reads it again soon; for another sheet it is the switch itself.
        self._repo.set_link(url=url.strip(), scope=check.link.scope, by=str(actor), now=now)
        self._audit.record(
            action="SHEET_LINK_CHANGED",
            entity_type="sheet_source",
            entity_id=ENTITY_ID,
            actor=str(actor),
            actor_kind=actor.kind.value,
            at=now,
            before={"url": before.url if before else None},
            after={"url": url.strip(), "scope": check.link.scope, "tasks_to_hide": check.will_hide},
        )
        state = self._repo.get()
        assert state is not None
        return state
