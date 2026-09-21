"""The audit hash chain algorithm -- pure, no database.

Persistence-layer concerns (concurrency, the advisory lock, actually reading
rows back) are covered separately in tests/integration/test_audit_sink.py,
since those genuinely need a real database to mean anything.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from interlock.domain.audit.chain import (
    AuditEntry,
    compute_entry_hash,
    verify_chain,
)

NOW = dt.datetime(2026, 9, 15, 17, 4, tzinfo=dt.UTC)


def _hash(**overrides: object) -> str:
    defaults: dict[str, object] = {
        "prev_hash": None,
        "actor": "kaif",
        "actor_kind": "USER",
        "action": "TASK_UPDATED",
        "entity_type": "task",
        "entity_id": "TSK-00104",
        "before": {"status": "PENDING"},
        "after": {"status": "COMPLETED"},
        "occurred_at": NOW,
        "correlation_id": "corr-1",
    }
    defaults.update(overrides)
    return compute_entry_hash(**defaults)  # type: ignore[arg-type]


def _entry(*, seq: int, prev_hash: str | None, entry_hash: str, **overrides: object) -> AuditEntry:
    defaults: dict[str, object] = {
        "id": f"entry-{seq}",
        "seq": seq,
        "actor": "kaif",
        "actor_kind": "USER",
        "action": "TASK_UPDATED",
        "entity_type": "task",
        "entity_id": "TSK-00104",
        "before": {"status": "PENDING"},
        "after": {"status": "COMPLETED"},
        "correlation_id": "corr-1",
        "occurred_at": NOW,
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
    }
    defaults.update(overrides)
    return AuditEntry(**defaults)  # type: ignore[arg-type]


class TestComputeEntryHash:
    def test_is_deterministic(self) -> None:
        assert _hash() == _hash()

    def test_sensitive_to_prev_hash(self) -> None:
        assert _hash(prev_hash="a" * 64) != _hash(prev_hash="b" * 64)

    def test_sensitive_to_action(self) -> None:
        assert _hash(action="TASK_UPDATED") != _hash(action="TASK_CREATED")

    def test_sensitive_to_after_payload(self) -> None:
        """This is the whole point: tampering with what was recorded must
        change the hash."""
        assert _hash(after={"status": "COMPLETED"}) != _hash(after={"status": "CANCELLED"})

    def test_sensitive_to_before_payload(self) -> None:
        assert _hash(before={"status": "PENDING"}) != _hash(before={"status": "BLOCKED"})

    def test_sensitive_to_occurred_at(self) -> None:
        later = NOW + dt.timedelta(minutes=1)
        assert _hash(occurred_at=NOW) != _hash(occurred_at=later)

    def test_sensitive_to_actor(self) -> None:
        assert _hash(actor="kaif") != _hash(actor="someone-else")

    def test_sensitive_to_correlation_id(self) -> None:
        assert _hash(correlation_id="corr-1") != _hash(correlation_id="corr-2")

    def test_none_before_is_distinct_from_empty_dict(self) -> None:
        """A creation event (before=None) must not hash the same as an edit
        that happens to record an empty before-state."""
        assert _hash(before=None) != _hash(before={})

    def test_naive_occurred_at_is_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="timezone-aware"):
            _hash(occurred_at=dt.datetime(2026, 9, 15, 17, 4))  # noqa: DTZ001


class TestVerifyChain:
    def test_empty_chain_is_intact(self) -> None:
        result = verify_chain([])
        assert result.intact
        assert result.entries_checked == 0
        assert result.first_break is None

    def test_a_single_valid_entry_is_intact(self) -> None:
        h = _hash(prev_hash=None)
        result = verify_chain([_entry(seq=1, prev_hash=None, entry_hash=h)])
        assert result.intact
        assert result.entries_checked == 1

    def test_a_valid_multi_entry_chain_is_intact(self) -> None:
        h1 = _hash(prev_hash=None, entity_id="TSK-00001")
        h2 = _hash(prev_hash=h1, entity_id="TSK-00002")
        h3 = _hash(prev_hash=h2, entity_id="TSK-00003")

        entries = [
            _entry(seq=1, prev_hash=None, entry_hash=h1, entity_id="TSK-00001"),
            _entry(seq=2, prev_hash=h1, entry_hash=h2, entity_id="TSK-00002"),
            _entry(seq=3, prev_hash=h2, entry_hash=h3, entity_id="TSK-00003"),
        ]
        result = verify_chain(entries)
        assert result.intact
        assert result.entries_checked == 3

    def test_first_entry_must_have_no_predecessor(self) -> None:
        """A chain that claims a predecessor for its very first entry is
        malformed regardless of what the hash math says."""
        h = _hash(prev_hash="not-actually-a-predecessor")
        result = verify_chain(
            [_entry(seq=1, prev_hash="not-actually-a-predecessor", entry_hash=h)]
        )
        assert not result.intact
        assert result.first_break is not None
        assert result.first_break.seq == 1

    def test_detects_a_tampered_payload(self) -> None:
        """The core guarantee: editing what an entry says it recorded must be
        detectable by recomputation, without trusting the stored hash."""
        h1 = _hash(prev_hash=None, entity_id="TSK-00001")
        entry = _entry(seq=1, prev_hash=None, entry_hash=h1, entity_id="TSK-00001")
        # Someone edited the row directly in the database after it was written.
        # dataclasses.replace(), not entry.__dict__ -- AuditEntry uses
        # slots=True, so it has no __dict__ to copy from.
        tampered = dataclasses.replace(entry, after={"status": "CANCELLED"})

        result = verify_chain([tampered])
        assert not result.intact
        assert result.first_break is not None
        assert "altered after being written" in result.first_break.reason

    def test_detects_a_broken_link(self) -> None:
        """Someone deleted the middle entry and left the others -- prev_hash
        no longer points at an actual predecessor's hash."""
        h1 = _hash(prev_hash=None, entity_id="TSK-00001")
        h2 = _hash(prev_hash=h1, entity_id="TSK-00002")
        h3 = _hash(prev_hash=h2, entity_id="TSK-00003")

        entries = [
            _entry(seq=1, prev_hash=None, entry_hash=h1, entity_id="TSK-00001"),
            # seq=2 (h2) is missing -- seq=3 still claims h2 as its predecessor.
            _entry(seq=3, prev_hash=h2, entry_hash=h3, entity_id="TSK-00003"),
        ]
        result = verify_chain(entries)
        assert not result.intact
        assert result.first_break is not None
        assert result.first_break.seq == 3

    def test_stops_at_the_first_break_rather_than_reporting_further(self) -> None:
        h1 = _hash(prev_hash=None, entity_id="TSK-00001")
        h2 = _hash(prev_hash=h1, entity_id="TSK-00002")

        entries = [
            _entry(seq=1, prev_hash=None, entry_hash=h1, entity_id="TSK-00001"),
            # Tampered: entry claims h2 but its payload no longer matches it.
            dataclasses.replace(
                _entry(seq=2, prev_hash=h1, entry_hash=h2, entity_id="TSK-00002"),
                after={"status": "TAMPERED"},
            ),
            # A third, otherwise-valid-looking entry after the break.
            _entry(seq=3, prev_hash=h2, entry_hash="c" * 64, entity_id="TSK-00003"),
        ]
        result = verify_chain(entries)
        assert not result.intact
        assert result.entries_checked == 1  # only the first entry was confirmed good
        assert result.first_break is not None
        assert result.first_break.seq == 2
