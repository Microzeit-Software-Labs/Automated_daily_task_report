"""The outbox between Interlock and the local WhatsApp agent.

The agent (``apps/agent/``, Node.js + Baileys) is a separate process that holds
the linked-device WhatsApp session. It shares no code and no socket with the
Python processes: both sides meet only here, in two tables, the same way the
rest of this system coordinates through durable rows rather than messages.

``whatsapp_agent_commands`` is written by Python (insert, and refresh an
expiry) and completed by the agent (claim, result). A row reaches ``DONE``
only for a *terminal* outcome -- a send the WhatsApp server acknowledged, or a
failure that retrying cannot fix -- mirroring exactly which outcomes
``MockWhatsAppProvider`` memoises. A transient failure puts the row back to
``PENDING`` instead, so a retry genuinely re-attempts.

``whatsapp_agent_status`` is one row the agent overwrites on a heartbeat, so
``health()`` is a single cheap read that never waits on the agent.

The agent-side SQL lives in ``apps/agent/sql/agent_queries.sql``; the Python
test harness runs that same file, so the contract tested is the one deployed.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import CheckConstraint, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from interlock.adapters.persistence.base import Base
from interlock.adapters.persistence.types import TIMESTAMPTZ, ULID_LENGTH, check_in
from interlock.domain.ports.whatsapp import ConnectionState, PairingState

OPS = ("resolve_group", "send_text", "test_send", "link_start", "link_cancel", "reconnect")
STATUSES = ("PENDING", "CLAIMED", "DONE")
STATUS_ROW_ID = "agent"


class WhatsAppAgentCommandRow(Base):
    __tablename__ = "whatsapp_agent_commands"
    __table_args__ = (
        CheckConstraint(check_in("op", OPS), name="ck_whatsapp_agent_commands_op"),
        CheckConstraint(check_in("status", STATUSES), name="ck_whatsapp_agent_commands_status"),
        # The idempotency backstop: one command per client_message_id, ever.
        # Only send_text carries one, hence partial.
        Index(
            "uq_whatsapp_agent_commands_client_message_id",
            "client_message_id",
            unique=True,
            postgresql_where="client_message_id IS NOT NULL",
        ),
        Index(
            "idx_whatsapp_agent_commands_pending",
            "created_at",
            postgresql_where="status = 'PENDING'",
        ),
    )

    id: Mapped[str] = mapped_column(String(ULID_LENGTH), primary_key=True)
    op: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    client_message_id: Mapped[str | None] = mapped_column(String(ULID_LENGTH), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    wa_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Persisted by the agent before it sends, so a resend after a crash
    reuses the same WhatsApp message key instead of minting a new one."""
    claimed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    """The agent never *starts* a command after this. Refreshed while a caller
    is still waiting, so a row Python has given up on cannot send later."""
    claimed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


class WhatsAppAgentStatusRow(Base):
    __tablename__ = "whatsapp_agent_status"
    __table_args__ = (
        CheckConstraint(check_in("state", ConnectionState), name="ck_whatsapp_agent_status_state"),
        CheckConstraint(
            check_in("pairing_state", PairingState), name="ck_whatsapp_agent_status_pairing_state"
        ),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=STATUS_ROW_ID)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    """Why the link is unusable, machine-readable (NOT_LINKED, LOGGED_OUT, ...);
    overwritten on every heartbeat, NULL while healthy."""
    account_jid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_successful_send_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMPTZ, nullable=True
    )
    last_canary_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    last_canary_ok: Mapped[bool | None] = mapped_column(nullable=True)
    pairing_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="IDLE", server_default="IDLE"
    )
    """The one phone-linking attempt that may be running. Written by the agent
    (``update_pairing`` in agent_queries.sql), polled by the UI."""
    pairing_id: Mapped[str | None] = mapped_column(String(ULID_LENGTH), nullable=True)
    pairing_qr: Mapped[str | None] = mapped_column(Text, nullable=True)
    pairing_qr_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    pairing_detail: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
