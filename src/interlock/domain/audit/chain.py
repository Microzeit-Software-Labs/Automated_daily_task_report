"""The audit hash chain.

Each entry's hash commits to its own fields *and* the previous entry's hash,
so editing or deleting any entry -- or reordering them -- changes every hash
that follows it. That is what turns "the audit log is immutable" from a
policy into something checkable: :func:`verify_chain` recomputes every hash
from scratch and disagrees with what is stored the moment anything changes.

This module is pure. It has no idea where entries come from or where the
chain's current tail lives -- that is the adapter's job (see
``adapters/persistence/audit_sink.py``), which is also where the concurrency
handling lives, since "what is the current tail" is a database question, not
a domain one.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from typing import Any

from interlock.domain.common.clock import ensure_aware


def compute_entry_hash(
    prev_hash: str | None,
    *,
    actor: str,
    actor_kind: str,
    action: str,
    entity_type: str,
    entity_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    occurred_at: dt.datetime,
    correlation_id: str | None,
) -> str:
    """The hash for one entry, given the entry before it in the chain.

    Deterministic: the same inputs always produce the same hash, which is what
    lets :func:`verify_chain` recompute and compare rather than trust.
    """
    ensure_aware(occurred_at, field="occurred_at")
    payload = {
        "prev_hash": prev_hash,
        "actor": actor,
        "actor_kind": actor_kind,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "before": before,
        "after": after,
        "occurred_at": occurred_at.isoformat(),
        "correlation_id": correlation_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class AuditEntry:
    """A read-only view of one stored audit row -- just enough to verify it.

    Deliberately its own type rather than an ORM row, so this module stays
    free of persistence imports.
    """

    id: str
    seq: int
    actor: str
    actor_kind: str
    action: str
    entity_type: str
    entity_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    correlation_id: str | None
    occurred_at: dt.datetime
    prev_hash: str | None
    entry_hash: str


@dataclasses.dataclass(frozen=True, slots=True)
class ChainBreak:
    """Where and how the chain stopped matching what it should be."""

    seq: int
    entry_id: str
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    intact: bool
    entries_checked: int
    first_break: ChainBreak | None = None


def verify_chain(entries: Sequence[AuditEntry]) -> ChainVerificationResult:
    """Recompute every hash in ``entries`` (ascending ``seq`` order) and
    confirm it matches what is stored, and that each entry's ``prev_hash``
    actually points at its predecessor's hash.

    Stops at the first mismatch: one broken link invalidates everything after
    it in a hash chain, so reporting further entries would not add
    information -- and would misleadingly suggest they were checked when they
    were not.
    """
    expected_prev: str | None = None
    for index, entry in enumerate(entries):
        if entry.prev_hash != expected_prev:
            return ChainVerificationResult(
                intact=False,
                entries_checked=index,
                first_break=ChainBreak(
                    seq=entry.seq,
                    entry_id=entry.id,
                    reason=(
                        "prev_hash does not match the preceding entry's hash "
                        f"(expected {expected_prev!r}, found {entry.prev_hash!r})"
                    ),
                ),
            )

        recomputed = compute_entry_hash(
            entry.prev_hash,
            actor=entry.actor,
            actor_kind=entry.actor_kind,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            before=entry.before,
            after=entry.after,
            occurred_at=entry.occurred_at,
            correlation_id=entry.correlation_id,
        )
        if recomputed != entry.entry_hash:
            return ChainVerificationResult(
                intact=False,
                entries_checked=index,
                first_break=ChainBreak(
                    seq=entry.seq,
                    entry_id=entry.id,
                    reason=(
                        "stored entry_hash does not match its own recomputed "
                        "hash -- this entry was altered after being written"
                    ),
                ),
            )

        expected_prev = entry.entry_hash

    return ChainVerificationResult(intact=True, entries_checked=len(entries))
