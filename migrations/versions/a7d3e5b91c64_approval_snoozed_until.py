"""approval_requests.snoozed_until -- "remind me again in 30 minutes".

The 09:00 / 17:00 popup is derived from the open review itself (see
domain/approvals/prompt.py), not from a separate notification record, so a
snooze is just a timestamp on the review. It lives in Postgres like every
other piece of schedule state: it survives a restart and can't duplicate.

Orthogonal to the approval state machine -- snoozing changes no state.

Revision ID: a7d3e5b91c64
Revises: f1a9c3d7e052
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3e5b91c64"
down_revision: Union[str, Sequence[str], None] = "f1a9c3d7e052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "approval_requests",
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("approval_requests", "snoozed_until")
