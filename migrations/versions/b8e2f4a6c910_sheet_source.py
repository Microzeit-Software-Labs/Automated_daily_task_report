"""sheet_source -- which Google Sheet the tasks come from, and how the last read went.

One row (``id = 1``). Replaces ``SHEET_IMPORT_URL`` in ``.env`` as the live
setting, so a link changed in the browser reaches the worker without a restart;
the environment value only seeds the row the first time.

No grant is needed: scripts/setup-database.ps1 sets default privileges that give
interlock_app SELECT/INSERT/UPDATE/DELETE on every table interlock_owner creates.

Revision ID: b8e2f4a6c910
Revises: c3b7e1f94a26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8e2f4a6c910"
down_revision: Union[str, Sequence[str], None] = "c3b7e1f94a26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sheet_source",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(length=120), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=200), nullable=False),
        sa.Column("import_pending", sa.Boolean(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_task_count", sa.Integer(), nullable=True),
        sa.Column("last_error_code", sa.String(length=30), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_sheet_source_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("sheet_source")
