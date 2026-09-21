"""The append-only, hash-chained audit log."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Identity, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ, ULID_LENGTH, check_in
from interlock.domain.common.actor import ActorKind


class AuditLogRow(Base):
    """One entry per significant action. Append-only.

    ``seq`` is a real database-generated identity sequence and is what the hash
    chain actually orders on -- wall-clock timestamps are not guaranteed unique
    or monotonic under concurrent writers, but a sequence is.

    UPDATE and DELETE on this table are revoked from the application role in
    the migration that creates it (see its ``op.execute(...)`` calls). Only
    INSERT remains possible, which is what makes "append-only" a property the
    database enforces rather than a convention application code has to honour.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(check_in("actor_kind", ActorKind), name="ck_audit_actor_kind"),
        Index("idx_audit_entity", "entity_type", "entity_id"),
        Index("idx_audit_correlation", "correlation_id"),
    )

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), unique=True, nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(ULID_LENGTH), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(ULID_LENGTH), nullable=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
