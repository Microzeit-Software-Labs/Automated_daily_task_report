"""Python's side of the whatsapp_agent_commands / whatsapp_agent_status outbox.

Owned exclusively by :mod:`interlock.adapters.whatsapp.local_agent`. Python
only ever **inserts** a command and **selects** a result -- it never claims,
completes, or resets a row; that is the agent's job alone, via
``apps/agent/sql/agent_queries.sql``. This asymmetry is what makes "the next
retry re-attaches to whatever the agent eventually does with the original
command" true without any special-casing on either side.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import WhatsAppAgentCommandRow, WhatsAppAgentStatusRow
from interlock.adapters.persistence.models.whatsapp_agent import STATUS_ROW_ID

_NOTIFY_CHANNEL = "whatsapp_agent_commands"


@dataclasses.dataclass(frozen=True, slots=True)
class CommandRecord:
    id: str
    op: str
    status: str
    client_message_id: str | None
    result: dict[str, Any] | None
    wa_message_id: str | None
    created_at: dt.datetime
    expires_at: dt.datetime


@dataclasses.dataclass(frozen=True, slots=True)
class AgentStatus:
    state: str
    detail: str
    agent_version: str | None
    last_successful_send_at: dt.datetime | None
    last_canary_at: dt.datetime | None
    last_canary_ok: bool | None
    updated_at: dt.datetime
    status_reason: str | None = None
    account_jid: str | None = None
    account_name: str | None = None
    pairing_state: str = "IDLE"
    pairing_id: str | None = None
    pairing_qr: str | None = None
    pairing_qr_at: dt.datetime | None = None
    pairing_detail: str = ""


class WhatsAppAgentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(
        self,
        *,
        command_id: str,
        op: str,
        payload: dict[str, Any],
        client_message_id: str | None,
        now: dt.datetime,
        expires_at: dt.datetime,
    ) -> None:
        """Create a command row, or -- if ``client_message_id`` already has
        one that is still ``PENDING`` -- refresh its expiry instead of
        creating a second row. A ``CLAIMED`` or ``DONE`` row is left
        completely untouched by the ``WHERE status = 'PENDING'`` guard on the
        conflict update: the caller already checked for a ``DONE`` result
        before ever calling this (see ``LocalAgentProvider.send_text``), and
        a ``CLAIMED`` row is mid-flight, not this call's to disturb.

        Always followed by a ``NOTIFY`` on the same connection, so the wake
        fires at commit time -- once, and only if this transaction actually
        lands.
        """
        stmt = insert(WhatsAppAgentCommandRow).values(
            id=command_id,
            op=op,
            payload=payload,
            client_message_id=client_message_id,
            status="PENDING",
            created_at=now,
            expires_at=expires_at,
        )
        if client_message_id is not None:
            # A partial unique index requires the same predicate repeated
            # here, or Postgres cannot infer which index the conflict
            # target refers to.
            stmt = stmt.on_conflict_do_update(
                index_elements=[WhatsAppAgentCommandRow.client_message_id],
                index_where=WhatsAppAgentCommandRow.client_message_id.is_not(None),
                set_={"expires_at": expires_at},
                where=(WhatsAppAgentCommandRow.status == "PENDING"),
            )
        self._session.execute(stmt)
        self._session.execute(text(f"SELECT pg_notify('{_NOTIFY_CHANNEL}', '')"))
        self._session.flush()

    def get_by_id(self, command_id: str) -> CommandRecord | None:
        row = self._session.get(WhatsAppAgentCommandRow, command_id)
        return None if row is None else _to_record(row)

    def get_done_by_client_message_id(self, client_message_id: str) -> CommandRecord | None:
        row = self._session.execute(
            select(WhatsAppAgentCommandRow).where(
                WhatsAppAgentCommandRow.client_message_id == client_message_id,
                WhatsAppAgentCommandRow.status == "DONE",
            )
        ).scalar_one_or_none()
        return None if row is None else _to_record(row)

    def get_status_with_staleness(
        self, *, now: dt.datetime, stale_after: dt.timedelta
    ) -> tuple[AgentStatus | None, bool]:
        """The heartbeat row, and whether it is too old to trust.

        Staleness is judged against ``now`` (the caller's injected
        :class:`~interlock.domain.common.clock.Clock`) rather than a second
        ``now()`` round trip -- the row's own ``updated_at`` is already a
        real database timestamp, so one read is enough.
        """
        row = self._session.get(WhatsAppAgentStatusRow, STATUS_ROW_ID)
        if row is None:
            return None, True
        status = _status_to_record(row)
        return status, (now - status.updated_at) > stale_after


def _to_record(row: WhatsAppAgentCommandRow) -> CommandRecord:
    return CommandRecord(
        id=row.id,
        op=row.op,
        status=row.status,
        client_message_id=row.client_message_id,
        result=row.result,
        wa_message_id=row.wa_message_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _status_to_record(row: WhatsAppAgentStatusRow) -> AgentStatus:
    return AgentStatus(
        state=row.state,
        detail=row.detail,
        agent_version=row.agent_version,
        last_successful_send_at=row.last_successful_send_at,
        last_canary_at=row.last_canary_at,
        last_canary_ok=row.last_canary_ok,
        updated_at=row.updated_at,
        status_reason=row.status_reason,
        account_jid=row.account_jid,
        account_name=row.account_name,
        pairing_state=row.pairing_state,
        pairing_id=row.pairing_id,
        pairing_qr=row.pairing_qr,
        pairing_qr_at=row.pairing_qr_at,
        pairing_detail=row.pairing_detail,
    )
