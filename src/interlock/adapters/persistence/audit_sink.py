"""PostgreSQL-backed audit sink.

Implements :class:`interlock.domain.ports.repositories.AuditSink`. Every write
takes a transaction-scoped advisory lock before reading the chain's current
tail and inserting the next entry.

Without that lock, two concurrent audit writes could both read the same tail
hash, both compute the same ``prev_hash``, and both insert -- forking the
chain instead of extending it. Postgres assigns each row's ``seq`` atomically
at insert time regardless, so :func:`verify_chain` would actually *catch* a
fork after the fact (the second inserted row's ``prev_hash`` would no longer
match the hash of the row immediately before it in ``seq`` order) -- but
catching corruption after it happens is far worse than preventing it, so the
lock exists to make forking impossible rather than merely detectable.

An advisory lock is used rather than locking the tail row directly
(``SELECT ... FOR UPDATE``) because that approach has no row to lock for the
very first entry ever written -- the table is empty. The advisory lock is keyed
on a fixed constant, so it serializes writers whether the table holds zero
rows or a million.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import AuditLogRow
from interlock.domain.audit.chain import (
    AuditEntry,
    ChainVerificationResult,
    compute_entry_hash,
    verify_chain,
)
from interlock.domain.common.ids import new_id

# Arbitrary but fixed: any value in the signed-bigint range works as an
# advisory lock key. Chosen once and never changed -- changing it would let
# old and new code take different locks and silently stop serializing each
# other.
_CHAIN_LOCK_KEY: Final = 782_991_004_417


class PostgresAuditSink:
    """Bound to one Session, so its writes land in whatever transaction that
    session is already part of -- see :func:`interlock.adapters.persistence
    .base.unit_of_work`. This is what makes an audit entry durable in the same
    transaction as the change it describes: both are on the same session, and
    either both commit or both roll back."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        actor: str,
        actor_kind: str,
        at: dt.datetime,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> str:
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHAIN_LOCK_KEY}
        )

        tail_hash = self._session.execute(
            text("SELECT entry_hash FROM audit_logs ORDER BY seq DESC LIMIT 1")
        ).scalar_one_or_none()

        entry_hash = compute_entry_hash(
            tail_hash,
            actor=actor,
            actor_kind=actor_kind,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            occurred_at=at,
            correlation_id=correlation_id,
        )

        self._session.add(
            AuditLogRow(
                id=new_id(),
                actor_id=actor,
                actor_kind=actor_kind,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                before=before,
                after=after,
                correlation_id=correlation_id,
                occurred_at=at,
                prev_hash=tail_hash,
                entry_hash=entry_hash,
            )
        )
        # Flush, not commit: surfaces a constraint violation immediately, while
        # staying fully rollback-able with whatever else this transaction does.
        self._session.flush()
        return entry_hash


def load_and_verify_chain(session: Session) -> ChainVerificationResult:
    """Read the entire audit log and confirm it is internally consistent.

    The read-only counterpart to :class:`PostgresAuditSink`: what the
    architecture calls the nightly chain-integrity job is this function, run
    on a schedule, alerting if the result is not intact.
    """
    rows = session.execute(select(AuditLogRow).order_by(AuditLogRow.seq)).scalars().all()
    entries = [
        AuditEntry(
            id=row.id,
            seq=row.seq,
            actor=row.actor_id,
            actor_kind=row.actor_kind,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            before=row.before,
            after=row.after,
            correlation_id=row.correlation_id,
            occurred_at=row.occurred_at,
            prev_hash=row.prev_hash,
            entry_hash=row.entry_hash,
        )
        for row in rows
    ]
    return verify_chain(entries)
