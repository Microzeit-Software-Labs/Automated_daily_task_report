"""report_snapshots.rendered_image -- the frozen PNG of an image-format report.

Nullable: text-format snapshots (and every snapshot taken before this) have
none. The table's UPDATE/DELETE revoke for the app role is untouched, so the
image is as immutable as the rest of the snapshot.

Revision ID: e5c2a7d9b418
Revises: d3b8e61a0f47
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5c2a7d9b418"
down_revision: Union[str, Sequence[str], None] = "d3b8e61a0f47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("report_snapshots", sa.Column("rendered_image", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("report_snapshots", "rendered_image")
