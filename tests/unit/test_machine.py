"""State machine invariants.

The two properties that matter most:

1. Only a human can approve a report for sending.
2. A fully-sent job is terminal, so a duplicate send is unreachable.
"""

from __future__ import annotations

import datetime as dt

import pytest

from interlock.domain.approvals.machine import (
    can_transition,
    is_terminal,
    legal_targets,
    roll_up_job_state,
    transition,
)
from interlock.domain.approvals.states import (
    ApprovalState,
    RecipientState,
    ShareJobState,
)
from interlock.domain.common.actor import Actor, ActorKind, agent_actor, system_actor
from interlock.domain.common.errors import (
    ApprovalRequiredError,
    IllegalTransitionError,
)
from tests.conftest import BRIEF_EVENING

HUMAN = Actor(kind=ActorKind.USER, id="u-1", label="kaif")
SCHEDULER = system_actor("scheduler")


class TestTheApprovalGate:
    """The product's central business rule, enforced mechanically."""

    def test_human_can_approve(self) -> None:
        record = transition(
            ApprovalState.READY,
            ApprovalState.APPROVED,
            entity_type="approval_request",
            entity_id="REV-00001",
            actor=HUMAN,
            at=BRIEF_EVENING,
        )
        assert record.to_state == "APPROVED"
        assert record.actor_kind == "USER"

    @pytest.mark.parametrize(
        "actor",
        [system_actor("scheduler"), system_actor("worker"), agent_actor("laptop-1")],
    )
    def test_non_human_actors_cannot_approve(self, actor: Actor) -> None:
        """The scheduler may prepare a report. It may never authorise sending it."""
        with pytest.raises(ApprovalRequiredError) as exc:
            transition(
                ApprovalState.READY,
                ApprovalState.APPROVED,
                entity_type="approval_request",
                entity_id="REV-00001",
                actor=actor,
                at=BRIEF_EVENING,
            )
        assert exc.value.code == "APPROVAL_REQUIRED"

    def test_approval_cannot_skip_review(self) -> None:
        """DRAFT -> APPROVED would bypass the human ever seeing the report."""
        with pytest.raises(IllegalTransitionError):
            transition(
                ApprovalState.DRAFT,
                ApprovalState.APPROVED,
                entity_type="approval_request",
                entity_id="REV-00001",
                actor=HUMAN,
                at=BRIEF_EVENING,
            )


class TestUpdateOnlyIsASuccess:
    def test_update_only_reaches_a_terminal_success_state(self) -> None:
        """"Update Only" is a real outcome, not an abandoned review."""
        assert can_transition(ApprovalState.USER_EDITING, ApprovalState.CLOSED_NO_SHARE)
        assert is_terminal(ApprovalState.CLOSED_NO_SHARE)

    def test_closed_no_share_never_reaches_approved(self) -> None:
        """Scenario 1: choosing Update Only must make sending unreachable."""
        assert legal_targets(ApprovalState.CLOSED_NO_SHARE) == frozenset()


class TestDuplicateSendsAreUnreachable:
    def test_sent_is_terminal(self) -> None:
        """Scenario 6. A fully-delivered job has nothing to retry."""
        assert is_terminal(ShareJobState.SENT)
        assert legal_targets(ShareJobState.SENT) == frozenset()

    def test_sent_cannot_be_resent(self) -> None:
        with pytest.raises(IllegalTransitionError):
            transition(
                ShareJobState.SENT,
                ShareJobState.SENDING,
                entity_type="share_job",
                entity_id="SHR-00204",
                actor=HUMAN,
                at=BRIEF_EVENING,
            )

    @pytest.mark.parametrize(
        "state", [ShareJobState.PARTIALLY_SENT, ShareJobState.FAILED]
    )
    def test_partial_and_total_failure_can_be_retried(
        self, state: ShareJobState
    ) -> None:
        """Scenario 8: retry must be possible when something actually failed."""
        assert can_transition(state, ShareJobState.SENDING)


class TestDeferral:
    def test_scheduled_can_defer(self) -> None:
        assert can_transition(ShareJobState.SCHEDULED, ShareJobState.DEFERRED)

    def test_deferred_requires_explicit_action(self) -> None:
        """A deferred job can be sent, rescheduled or discarded -- never ignored
        back into SCHEDULED by the system on its own."""
        assert legal_targets(ShareJobState.DEFERRED) == frozenset(
            {
                ShareJobState.SCHEDULED,
                ShareJobState.SENDING,
                ShareJobState.CANCELLED,
            }
        )

    def test_a_human_may_send_a_deferred_report(self) -> None:
        record = transition(
            ShareJobState.DEFERRED,
            ShareJobState.SENDING,
            entity_type="share_job",
            entity_id="SHR-00204",
            actor=HUMAN,
            at=BRIEF_EVENING,
            reason="user reviewed and sent after deferral",
        )
        assert record.reason is not None


class TestRollUp:
    """Job outcome is computed from recipients, never asserted."""

    def test_all_delivered_is_sent(self) -> None:
        assert (
            roll_up_job_state([RecipientState.SENT, RecipientState.SENT])
            is ShareJobState.SENT
        )

    def test_scenario_8_two_sent_one_failed(self) -> None:
        """SI Team and Management succeed, AI Engineering fails."""
        assert (
            roll_up_job_state(
                [RecipientState.SENT, RecipientState.SENT, RecipientState.FAILED]
            )
            is ShareJobState.PARTIALLY_SENT
        )

    def test_all_failed_is_failed(self) -> None:
        assert (
            roll_up_job_state([RecipientState.FAILED, RecipientState.FAILED])
            is ShareJobState.FAILED
        )

    def test_still_in_flight_stays_sending(self) -> None:
        assert (
            roll_up_job_state([RecipientState.SENT, RecipientState.SENDING])
            is ShareJobState.SENDING
        )
        assert (
            roll_up_job_state([RecipientState.SENT, RecipientState.QUEUED])
            is ShareJobState.SENDING
        )

    def test_skipped_groups_do_not_count_as_failures(self) -> None:
        """A group disabled between approval and send is not a delivery failure."""
        assert (
            roll_up_job_state([RecipientState.SENT, RecipientState.SKIPPED])
            is ShareJobState.SENT
        )

    def test_all_skipped_is_cancelled_not_sent(self) -> None:
        assert (
            roll_up_job_state([RecipientState.SKIPPED, RecipientState.SKIPPED])
            is ShareJobState.CANCELLED
        )

    def test_empty_recipient_list_is_a_failure(self) -> None:
        """Defensive: a job with no recipients should never look successful."""
        assert roll_up_job_state([]) is ShareJobState.FAILED


class TestRecipientLifecycle:
    def test_queued_to_sending_is_the_claim(self) -> None:
        assert can_transition(RecipientState.QUEUED, RecipientState.SENDING)

    def test_failed_recipient_requeues_for_retry(self) -> None:
        assert can_transition(RecipientState.FAILED, RecipientState.QUEUED)

    def test_delivered_recipient_is_terminal(self) -> None:
        """Scenario 8: retrying a job must never re-send an already-sent group."""
        assert is_terminal(RecipientState.SENT)
        assert legal_targets(RecipientState.SENT) == frozenset()


class TestTransitionRecords:
    def test_record_captures_the_audit_fields(self) -> None:
        record = transition(
            ApprovalState.REVIEW_PENDING,
            ApprovalState.USER_EDITING,
            entity_type="approval_request",
            entity_id="REV-00001",
            actor=HUMAN,
            at=BRIEF_EVENING,
            reason="opened review",
        )
        payload = record.as_dict()
        assert payload["from_state"] == "REVIEW_PENDING"
        assert payload["to_state"] == "USER_EDITING"
        assert payload["actor"] == "kaif"
        assert payload["reason"] == "opened review"
        assert payload["at"] is not None

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            transition(
                ApprovalState.DRAFT,
                ApprovalState.REVIEW_PENDING,
                entity_type="approval_request",
                entity_id="REV-00001",
                actor=SCHEDULER,
                at=dt.datetime(2026, 9, 15, 17, 0),  # noqa: DTZ001
            )


class TestTablesAreComplete:
    @pytest.mark.parametrize(
        "enum_cls", [ApprovalState, ShareJobState, RecipientState]
    )
    def test_every_state_appears_in_its_table(self, enum_cls: type) -> None:
        """A state with no table entry would raise KeyError at runtime."""
        for state in enum_cls:
            assert isinstance(legal_targets(state), frozenset)

    @pytest.mark.parametrize(
        "enum_cls", [ApprovalState, ShareJobState, RecipientState]
    )
    def test_every_target_is_a_known_state(self, enum_cls: type) -> None:
        for state in enum_cls:
            for target in legal_targets(state):
                assert isinstance(target, enum_cls)
