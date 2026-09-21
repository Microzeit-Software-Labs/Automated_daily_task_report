"""HTTP-level replay protection."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import CheckConstraint, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ, check_in

STATES = ("IN_PROGRESS", "COMPLETED")


class IdempotencyKeyRow(Base):
    """The primary key IS the caller's idempotency key.

    A second INSERT with the same key fails at the database, which is what
    makes "click twice" safe even before any application-level replay logic
    runs. ``request_hash`` catches the other failure mode: the same key reused
    for a genuinely different request is rejected rather than silently treated
    as a repeat of something it is not.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        CheckConstraint(check_in("state", STATES), name="ck_idempotency_state"),
        Index("idx_idempotency_expires", "expires_at"),
    )

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="IN_PROGRESS")
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
