"""task source sheet_import

Revision ID: d3b8e61a0f47
Revises: c7a41f9e2b30
Create Date: 2026-09-29 17:00:00.000000

Adds SHEET_IMPORT to the allowed tasks.source values, for tasks mirrored
read-only from a hand-kept Google Sheet (services/sheet_import_service.py).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d3b8e61a0f47"
down_revision: Union[str, Sequence[str], None] = "c7a41f9e2b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "source IN ('APP', 'GOOGLE_SHEETS', 'EXCEL', 'IMPORT')"
_NEW = "source IN ('APP', 'GOOGLE_SHEETS', 'EXCEL', 'IMPORT', 'SHEET_IMPORT')"


def upgrade() -> None:
    op.drop_constraint("ck_tasks_source", "tasks", type_="check")
    op.create_check_constraint("ck_tasks_source", "tasks", _NEW)


def downgrade() -> None:
    # Fails loudly if SHEET_IMPORT rows exist, rather than silently leaving
    # rows the old constraint would reject -- delete or reassign them first.
    op.drop_constraint("ck_tasks_source", "tasks", type_="check")
    op.create_check_constraint("ck_tasks_source", "tasks", _OLD)
