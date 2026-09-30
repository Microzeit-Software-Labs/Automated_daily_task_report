"""share_jobs.deferred_reason: allow WHATSAPP_DISCONNECTED.

A report held back because WhatsApp was down until it was too late to send
(services/scheduler_service.py) is recorded with its own reason, so the UI can
say so instead of a generic "stale".

Revision ID: f1a9c3d7e052
Revises: e5c2a7d9b418
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f1a9c3d7e052"
down_revision: Union[str, Sequence[str], None] = "e5c2a7d9b418"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = ("CROSSED_DAY_BOUNDARY", "SNAPSHOT_STALE", "DATA_DRIFTED")
_NEW = (*_OLD, "WHATSAPP_DISCONNECTED")


def _check(values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"deferred_reason IS NULL OR deferred_reason IN ({listed})"


def upgrade() -> None:
    op.drop_constraint("ck_share_job_deferred_reason", "share_jobs", type_="check")
    op.create_check_constraint("ck_share_job_deferred_reason", "share_jobs", _check(_NEW))


def downgrade() -> None:
    # Jobs already held back for this reason would violate the old constraint.
    op.execute(
        "UPDATE share_jobs SET deferred_reason = 'SNAPSHOT_STALE' "
        "WHERE deferred_reason = 'WHATSAPP_DISCONNECTED'"
    )
    op.drop_constraint("ck_share_job_deferred_reason", "share_jobs", type_="check")
    op.create_check_constraint("ck_share_job_deferred_reason", "share_jobs", _check(_OLD))
