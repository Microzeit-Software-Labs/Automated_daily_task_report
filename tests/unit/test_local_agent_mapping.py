"""Pure row/result -> port-type mapping in LocalAgentProvider.

No database, no agent -- these are free functions with real branching
(success vs. failure, present vs. absent optional fields), so they are worth
testing directly rather than only through the heavier integration tests.
"""

from __future__ import annotations

import datetime as dt

from interlock.adapters.persistence.whatsapp_agent_repository import CommandRecord
from interlock.adapters.whatsapp.local_agent import _candidates_from_result, _outcome_from_record
from interlock.domain.ports.whatsapp import DeliveryAck, ErrorClass
from tests.conftest import BRIEF_EVENING


def make_record(
    *,
    op: str = "send_text",
    status: str = "DONE",
    client_message_id: str | None = "m-1",
    result: dict[str, object] | None = None,
    wa_message_id: str | None = None,
) -> CommandRecord:
    return CommandRecord(
        id="cmd-1",
        op=op,
        status=status,
        client_message_id=client_message_id,
        result=result,
        wa_message_id=wa_message_id,
        created_at=BRIEF_EVENING,
        expires_at=BRIEF_EVENING + dt.timedelta(seconds=20),
    )


class TestOutcomeFromRecordSuccess:
    def test_maps_accepted_result(self) -> None:
        record = make_record(
            result={"accepted": True, "provider_message_id": "wamid-1", "ack": "SERVER"}
        )
        outcome = _outcome_from_record(record, deduplicated=False)
        assert outcome.accepted
        assert outcome.provider_message_id == "wamid-1"
        assert outcome.ack is DeliveryAck.SERVER
        assert outcome.client_message_id == "m-1"
        assert not outcome.deduplicated

    def test_falls_back_to_wa_message_id_when_result_omits_provider_message_id(
        self,
    ) -> None:
        record = make_record(
            result={"accepted": True, "ack": "DEVICE"}, wa_message_id="wamid-fallback"
        )
        outcome = _outcome_from_record(record, deduplicated=False)
        assert outcome.provider_message_id == "wamid-fallback"

    def test_deduplicated_flag_is_set_by_the_caller_not_the_record(self) -> None:
        record = make_record(result={"accepted": True, "provider_message_id": "wamid-1"})
        assert _outcome_from_record(record, deduplicated=True).deduplicated
        assert not _outcome_from_record(record, deduplicated=False).deduplicated


class TestOutcomeFromRecordFailure:
    def test_maps_a_failure_result(self) -> None:
        record = make_record(
            result={
                "accepted": False,
                "error_code": "GROUP_NOT_FOUND",
                "error_detail": "no longer a member",
                "error_class": "PERMANENT",
            }
        )
        outcome = _outcome_from_record(record, deduplicated=False)
        assert not outcome.accepted
        assert outcome.error_code == "GROUP_NOT_FOUND"
        assert outcome.error_class is ErrorClass.PERMANENT
        assert not outcome.retryable

    def test_missing_result_defaults_to_a_permanent_unknown_failure(self) -> None:
        """Never crashes on a malformed/empty result -- degrades to the safe
        side (not retryable) rather than raising out of the provider."""
        record = make_record(result=None)
        outcome = _outcome_from_record(record, deduplicated=False)
        assert not outcome.accepted
        assert outcome.error_code == "UNKNOWN"
        assert outcome.error_class is ErrorClass.PERMANENT


class TestCandidatesFromResult:
    def test_maps_every_candidate(self) -> None:
        result = {
            "candidates": [
                {
                    "external_jid": "a@g.us",
                    "display_name": "A",
                    "member_count": 3,
                    "confidence": 1.0,
                },
                {"external_jid": "b@g.us", "display_name": "B", "confidence": 0.6},
            ]
        }
        candidates = _candidates_from_result(result)
        assert len(candidates) == 2
        assert candidates[0].external_jid == "a@g.us"
        assert candidates[0].member_count == 3
        assert candidates[1].member_count is None

    def test_none_result_returns_empty(self) -> None:
        assert _candidates_from_result(None) == ()

    def test_result_with_no_candidates_key_returns_empty(self) -> None:
        assert _candidates_from_result({}) == ()
