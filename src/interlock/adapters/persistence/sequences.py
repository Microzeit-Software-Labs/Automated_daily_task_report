"""Display-id sequences.

Backed by real PostgreSQL sequences rather than, say, counting existing rows:
a count-based scheme collides under concurrent inserts and reuses numbers when
rows are deleted. A sequence is atomic, monotonic and never reused, which is
what a human-facing id like ``TSK-00104`` needs to keep meaning "the 104th
task ever created" rather than "task number 104 as of whenever I last
counted."
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

_SEQUENCES: Final[dict[str, str]] = {
    "task": "task_display_seq",
    "review": "review_display_seq",
    "share": "share_display_seq",
}


class PostgresSequences:
    def __init__(self, session: Session) -> None:
        self._session = session

    def next(self, kind: str) -> int:
        sequence_name = _SEQUENCES[kind]  # KeyError on an unknown kind -- fail loudly
        result = self._session.execute(
            text("SELECT nextval(:name)"), {"name": sequence_name}
        ).scalar_one()
        return int(result)
