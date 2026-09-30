"""WhatsApp link management: who is linked, why it isn't, and the pairing attempt.

``whatsapp_agent_status`` gains the linked account (kept after a disconnect so
the UI can say "last connected as ..."), a machine-readable reason the link is
unusable, and the progress of the one phone-linking attempt that may be
running (the QR code and its state), which the UI polls.

``whatsapp_agent_commands`` learns three operations: ``link_start``,
``link_cancel`` and ``reconnect``. No new grants are needed: the agent role
already has SELECT/INSERT/UPDATE on both whole tables.

Revision ID: c3b7e1f94a26
Revises: a7d3e5b91c64
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3b7e1f94a26"
down_revision: Union[str, Sequence[str], None] = "a7d3e5b91c64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_OPS = ("resolve_group", "send_text", "test_send")
_NEW_OPS = (*_OLD_OPS, "link_start", "link_cancel", "reconnect")
_PAIRING_STATES = (
    "IDLE",
    "STARTING",
    "WAITING_FOR_SCAN",
    "SCANNED",
    "SUCCEEDED",
    "EXPIRED",
    "CANCELLED",
    "FAILED",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    status = "whatsapp_agent_status"
    op.add_column(status, sa.Column("status_reason", sa.String(length=30), nullable=True))
    op.add_column(status, sa.Column("account_jid", sa.String(length=100), nullable=True))
    op.add_column(status, sa.Column("account_name", sa.String(length=200), nullable=True))
    op.add_column(
        status,
        sa.Column("pairing_state", sa.String(length=20), nullable=False, server_default="IDLE"),
    )
    op.add_column(status, sa.Column("pairing_id", sa.String(length=26), nullable=True))
    op.add_column(status, sa.Column("pairing_qr", sa.Text(), nullable=True))
    op.add_column(status, sa.Column("pairing_qr_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        status,
        sa.Column("pairing_detail", sa.Text(), nullable=False, server_default=""),
    )
    op.create_check_constraint(
        "ck_whatsapp_agent_status_pairing_state", status, _in("pairing_state", _PAIRING_STATES)
    )

    commands = "whatsapp_agent_commands"
    op.drop_constraint("ck_whatsapp_agent_commands_op", commands, type_="check")
    op.create_check_constraint("ck_whatsapp_agent_commands_op", commands, _in("op", _NEW_OPS))


def downgrade() -> None:
    commands = "whatsapp_agent_commands"
    # Rows for operations the old constraint doesn't know would block it.
    op.execute("DELETE FROM whatsapp_agent_commands WHERE op IN ('link_start', 'link_cancel', 'reconnect')")
    op.drop_constraint("ck_whatsapp_agent_commands_op", commands, type_="check")
    op.create_check_constraint("ck_whatsapp_agent_commands_op", commands, _in("op", _OLD_OPS))

    status = "whatsapp_agent_status"
    op.drop_constraint("ck_whatsapp_agent_status_pairing_state", status, type_="check")
    for column in (
        "pairing_detail",
        "pairing_qr_at",
        "pairing_qr",
        "pairing_id",
        "pairing_state",
        "account_name",
        "account_jid",
        "status_reason",
    ):
        op.drop_column(status, column)
