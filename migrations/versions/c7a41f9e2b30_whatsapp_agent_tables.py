"""whatsapp agent tables

Revision ID: c7a41f9e2b30
Revises: ef1d9dacf6ad
Create Date: 2026-09-29 10:00:00.000000

Deliberately no GRANT to interlock_agent here: that role is optional (only
created by scripts/setup-agent-role.ps1 when the agent is opted into), and a
GRANT naming a role that does not exist would fail this migration on every
database without it -- interlock_test included. The setup script grants on
these two tables itself. interlock_app needs nothing: it already receives
SELECT/INSERT/UPDATE/DELETE on every new table via the default privileges
scripts/setup-database.ps1 set up.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7a41f9e2b30"
down_revision: Union[str, Sequence[str], None] = "ef1d9dacf6ad"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_agent_commands",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("op", sa.String(length=20), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("client_message_id", sa.String(length=26), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("wa_message_id", sa.String(length=100), nullable=True),
        sa.Column("claimed_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "op IN ('resolve_group', 'send_text', 'test_send')",
            name="ck_whatsapp_agent_commands_op",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CLAIMED', 'DONE')",
            name="ck_whatsapp_agent_commands_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_whatsapp_agent_commands_client_message_id",
        "whatsapp_agent_commands",
        ["client_message_id"],
        unique=True,
        postgresql_where="client_message_id IS NOT NULL",
    )
    op.create_index(
        "idx_whatsapp_agent_commands_pending",
        "whatsapp_agent_commands",
        ["created_at"],
        unique=False,
        postgresql_where="status = 'PENDING'",
    )
    op.create_table(
        "whatsapp_agent_status",
        sa.Column("id", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("agent_version", sa.String(length=50), nullable=True),
        sa.Column("last_successful_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_canary_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_canary_ok", sa.Boolean(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('CONNECTED', 'CONNECTING', 'LOGIN_REQUIRED', 'UNAVAILABLE', "
            "'AUTOMATION_ERROR')",
            name="ck_whatsapp_agent_status_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("whatsapp_agent_status")
    op.drop_index(
        "idx_whatsapp_agent_commands_pending",
        table_name="whatsapp_agent_commands",
        postgresql_where="status = 'PENDING'",
    )
    op.drop_index(
        "uq_whatsapp_agent_commands_client_message_id",
        table_name="whatsapp_agent_commands",
        postgresql_where="client_message_id IS NOT NULL",
    )
    op.drop_table("whatsapp_agent_commands")
