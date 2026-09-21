"""Configured delivery targets."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ, ULID_LENGTH


class WhatsAppGroupRow(Base):
    """A group a report can be sent to.

    ``external_jid`` is pinned permanently once a human confirms a candidate
    from ``WhatsAppProvider.resolve_group`` -- it is never re-resolved by name
    at send time, so a group rename or a second similarly-named group cannot
    silently redirect a report.
    """

    __tablename__ = "whatsapp_groups"

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    external_jid: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    default_morning: Mapped[bool] = mapped_column(nullable=False, default=False)
    default_evening: Mapped[bool] = mapped_column(nullable=False, default=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
