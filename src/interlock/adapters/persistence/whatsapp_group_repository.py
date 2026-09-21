"""PostgreSQL-backed WhatsApp group configuration."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import WhatsAppGroupRow
from interlock.domain.common.errors import NotFoundError
from interlock.domain.common.ids import new_id
from interlock.domain.sharing.group import WhatsAppGroup


class WhatsAppGroupRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, group_id: str) -> WhatsAppGroup | None:
        row = self._session.get(WhatsAppGroupRow, group_id)
        return None if row is None else _to_entity(row)

    def get_by_jid(self, external_jid: str) -> WhatsAppGroup | None:
        row = self._session.execute(
            select(WhatsAppGroupRow).where(WhatsAppGroupRow.external_jid == external_jid)
        ).scalar_one_or_none()
        return None if row is None else _to_entity(row)

    def list_groups(self, *, enabled_only: bool = False) -> list[WhatsAppGroup]:
        # Named list_groups, not list: a method named `list` on this class
        # shadows the `list` builtin for other methods' annotations in the
        # same class body (e.g. list_by_ids's own `-> list[WhatsAppGroup]`)
        # under `from __future__ import annotations` lazy evaluation.
        stmt = select(WhatsAppGroupRow).order_by(WhatsAppGroupRow.display_name)
        if enabled_only:
            stmt = stmt.where(WhatsAppGroupRow.enabled.is_(True))
        rows = self._session.execute(stmt).scalars().all()
        return [_to_entity(row) for row in rows]

    def list_by_ids(self, group_ids: list[str]) -> list[WhatsAppGroup]:
        if not group_ids:
            return []
        rows = self._session.execute(
            select(WhatsAppGroupRow).where(WhatsAppGroupRow.id.in_(group_ids))
        ).scalars().all()
        return [_to_entity(row) for row in rows]

    def add(
        self,
        *,
        display_name: str,
        external_jid: str,
        now: dt.datetime,
        description: str = "",
        default_morning: bool = False,
        default_evening: bool = False,
    ) -> WhatsAppGroup:
        row = WhatsAppGroupRow(
            id=new_id(),
            display_name=display_name,
            external_jid=external_jid,
            description=description,
            enabled=True,
            default_morning=default_morning,
            default_evening=default_evening,
            resolved_at=now,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _to_entity(row)

    def mark_used(self, group_id: str, *, at: dt.datetime) -> None:
        row = self._session.get(WhatsAppGroupRow, group_id)
        if row is None:
            raise NotFoundError(f"WhatsApp group {group_id} does not exist.", group_id=group_id)
        row.last_used_at = at
        row.updated_at = at
        self._session.flush()

    def set_enabled(self, group_id: str, *, enabled: bool, at: dt.datetime) -> WhatsAppGroup:
        row = self._session.get(WhatsAppGroupRow, group_id)
        if row is None:
            raise NotFoundError(f"WhatsApp group {group_id} does not exist.", group_id=group_id)
        row.enabled = enabled
        row.updated_at = at
        self._session.flush()
        return _to_entity(row)


def _to_entity(row: WhatsAppGroupRow) -> WhatsAppGroup:
    return WhatsAppGroup(
        id=row.id,
        display_name=row.display_name,
        external_jid=row.external_jid,
        enabled=row.enabled,
        description=row.description,
        default_morning=row.default_morning,
        default_evening=row.default_evening,
        last_used_at=row.last_used_at,
        resolved_at=row.resolved_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
