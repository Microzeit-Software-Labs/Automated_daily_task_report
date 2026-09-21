"""The users table.

Phase 1 has no login flow -- this table exists so that
``share_jobs.approved_by_user_id`` and audit actor ids can be real foreign keys
rather than free-floating strings. Authentication is built in Phase 4; the
``role`` column already carries the vocabulary the approval gate needs
(only ``approver`` and ``admin`` may move a report into ``APPROVED``).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ, ULID_LENGTH, check_in

ROLES = ("viewer", "editor", "approver", "admin")


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(check_in("role", ROLES), name="ck_users_role"),)

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="editor")
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
