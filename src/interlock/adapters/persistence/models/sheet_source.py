"""Which Google Sheet the tasks are read from, and how the last read went.

One row, ever (``id = 1``). The link lives here, not in ``.env``: the API and
the worker are separate processes, and a link changed in the browser has to
reach the worker without a restart. ``.env``'s ``SHEET_IMPORT_URL`` only seeds
this row the first time (see ``SheetSourceService.current``).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ


class SheetSourceRow(Base):
    __tablename__ = "sheet_source"
    __table_args__ = (CheckConstraint("id = 1", name="ck_sheet_source_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(120), nullable=True)
    """``<spreadsheet id>:<gid>``; what imported tasks' refs are scoped by."""
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(200), nullable=False)

    import_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Set when the link changes: the worker imports on its next tick instead of
    waiting out the usual interval, then clears it."""

    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    last_task_count: Mapped[int | None] = mapped_column(nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
