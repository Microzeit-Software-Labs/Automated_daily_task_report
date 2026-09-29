"""Bidirectional reconciliation between a task and its spreadsheet peer.

Pure decision logic only -- no I/O, no adapter imports. The service layer
(:mod:`interlock.services.sheet_sync_service`) is what actually reads a
sheet, loads a task, and acts on the answers this module gives it.

The spreadsheet is a peer, not a replica of convenience: a task edited in the
sheet is exactly as real as one edited in the app. What makes that safe is
never guessing when both sides changed since they were last known to agree --
:func:`decide_action` is the whole of that guess-free rule, expressed as a
truth table rather than buried in branching service code.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from enum import StrEnum
from typing import Any

from interlock.domain.tasks.entities import EDITABLE_FIELDS, Task


class SyncAction(StrEnum):
    NOOP = "NOOP"
    """Neither side changed since they last agreed. Nothing to do."""

    PUSH = "PUSH"
    """Only the database changed. Write the task's current fields to the sheet."""

    PULL = "PULL"
    """Only the sheet changed. Apply the sheet's fields to the task."""

    CONFLICT = "CONFLICT"
    """Both changed. Touch neither side -- park it for a human to resolve."""


class ConflictResolution(StrEnum):
    """A human's decision on a parked collision. Shared by the service layer,
    the API schema, and the database CHECK constraint (``models/sync.py``'s
    ``RESOLUTIONS``) so the three cannot drift to accept different values."""

    KEPT_DB = "kept_db"
    KEPT_SHEET = "kept_sheet"


def decide_action(*, db_changed: bool, sheet_changed: bool) -> SyncAction:
    """The reconciliation truth table, and nothing more than it.

    Deliberately exhaustive over the four boolean combinations rather than
    written as nested ``if``s, so every branch is a one-line, individually
    testable fact instead of something to trace through.
    """
    if db_changed and sheet_changed:
        return SyncAction.CONFLICT
    if db_changed:
        return SyncAction.PUSH
    if sheet_changed:
        return SyncAction.PULL
    return SyncAction.NOOP


def editable_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Restrict an arbitrary field mapping to the sheet-syncable subset.

    Used on both sides: a task's ``snapshot_fields()`` carries system-owned
    fields (``id``, ``version``, ``created_at``, ...) that never appear as a
    sheet column, and a freshly parsed sheet row should never accidentally
    smuggle in a column it does not actually have a value for.
    """
    return {key: value for key, value in fields.items() if key in EDITABLE_FIELDS}


def task_content_fields(task: Task) -> dict[str, Any]:
    """The sheet-syncable, JSON-shaped subset of a task's current fields.

    Reuses :meth:`Task.snapshot_fields` rather than re-deriving the
    JSON-shaping rules (enum-to-string, date-to-ISO, tuple-to-list) a second
    time, so this can never quietly drift from what audit/history already do.
    """
    return editable_fields(task.snapshot_fields())


def content_hash(fields: dict[str, Any]) -> str:
    """A deterministic fingerprint of a sheet-syncable field mapping.

    Same canonicalization idiom as
    :func:`interlock.domain.audit.chain.compute_entry_hash` -- sorted keys,
    compact separators -- so that the same logical values always hash the
    same regardless of dict insertion order or which side (task vs. parsed
    sheet row) produced them.
    """
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_FREE_TEXT_FIELDS = frozenset({"title", "description", "remarks"})


def canonicalize(fields: dict[str, Any]) -> dict[str, Any]:
    """Normalize free-text fields so the same logical value hashes
    identically regardless of which side produced it.

    ``adapters.sheets.columns.parse_field_value`` strips whitespace on every
    cell it reads back; a task's own stored fields carry no such guarantee
    (``Task`` only rejects a *blank* title, it does not trim one). Without
    this, a title/description/remarks value with incidental surrounding
    whitespace would hash differently depending on which side produced it,
    and -- since neither side's hash routine strips on write -- never
    converge. Idempotent, so calling it on an already-canonical mapping
    (or twice, as :func:`resolve_baseline`'s caller does for both sides) is
    harmless.
    """
    result = dict(fields)
    for name in _FREE_TEXT_FIELDS:
        value = result.get(name)
        if isinstance(value, str):
            result[name] = value.strip()
    return result


@dataclasses.dataclass(frozen=True, slots=True)
class SyncState:
    """The reconciliation cursor for one task -- what was true as of the last
    tick that successfully agreed both sides matched."""

    task_id: str
    last_synced_version: int
    last_synced_content_hash: str
    last_synced_at: dt.datetime
    last_known_row_index: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Baseline:
    """What both sides last agreed on -- either the durable cursor, or, when
    that is missing, the sheet row's own ``_sys`` stamp."""

    version: int
    content_hash: str


def resolve_baseline(
    *,
    state: SyncState | None,
    stamped_version: int | None,
    stamped_content_hash: str | None,
) -> Baseline | None:
    """The durable cursor (``SyncRepository``'s :class:`SyncState`) wins when
    it exists. Otherwise fall back to the row's own ``_sys`` stamp -- written
    on every push, so a row already carrying one has genuinely been synced
    before even though *this* task's cursor row is missing (a restored
    database, or the first real tick after switching
    ``SHEETS_PROVIDER=mock`` to ``google``). ``None`` -- no baseline at all,
    meaning both sides count as changed -- only when neither is usable,
    which is genuine first contact between this task and this row.

    A stamp is only usable whole: a version with no hash, or a hash with no
    version, cannot happen from a write this service ever made, so a partial
    stamp is treated the same as no stamp rather than guessed at.
    """
    if state is not None:
        return Baseline(
            version=state.last_synced_version, content_hash=state.last_synced_content_hash
        )
    if stamped_version is not None and stamped_content_hash is not None:
        return Baseline(version=stamped_version, content_hash=stamped_content_hash)
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class SyncConflict:
    """A parked collision: both sides changed, so neither was touched."""

    id: str
    task_id: str
    detected_at: dt.datetime
    db_value: dict[str, Any]
    external_value: dict[str, Any] | None
    """``None`` means the row vanished from the sheet -- never treated as a
    deletion, per the architecture doc's R5."""
    resolution: str | None = None
    resolved_by: str | None = None
    resolved_at: dt.datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.resolution is None
