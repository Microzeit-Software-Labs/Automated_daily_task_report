"""display id sequences

Revision ID: a1e5b6f2c3d4
Revises: bdffd15a31c5
Create Date: 2026-09-16 15:00:00.000000

Real PostgreSQL sequences back the human-facing display ids (TSK-00104,
REV-00012, SHR-00204) rather than counting existing rows: a count-based
scheme collides under concurrent inserts and reuses numbers when rows are
deleted. A sequence is atomic, monotonic, and never reused.

interlock_app already receives USAGE and SELECT on sequences created by
interlock_owner by default (see the ALTER DEFAULT PRIVILEGES statements in
scripts/setup-database.ps1), so no additional grant is needed here.
"""

from __future__ import annotations

from alembic import op

revision: str = "a1e5b6f2c3d4"
down_revision: str | None = "bdffd15a31c5"
branch_labels: str | None = None
depends_on: str | None = None

_SEQUENCES = ("task_display_seq", "review_display_seq", "share_display_seq")


def upgrade() -> None:
    for name in _SEQUENCES:
        op.execute(f"CREATE SEQUENCE {name} AS bigint START WITH 1 INCREMENT BY 1")


def downgrade() -> None:
    for name in _SEQUENCES:
        op.execute(f"DROP SEQUENCE IF EXISTS {name}")
