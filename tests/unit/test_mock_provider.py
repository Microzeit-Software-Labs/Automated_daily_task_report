"""The mock provider's own contract.

Worth testing carefully rather than trusting: every acceptance scenario depends
on this behaving like a real provider. A mock that quietly allows duplicate
sends would let Scenario 6 pass while the real system is broken.
"""

from __future__ import annotations

import datetime as dt

import pytest

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from interlock.domain.ports.whatsapp import (
    ConnectionState,
    ErrorClass,
    WhatsAppProvider,
)
from tests.conftest import BRIEF_EVENING

SI_TEAM = "120363000000000001@g.us"
MANAGEMENT = "120363000000000002@g.us"


@pytest.fixture
def provider() -> MockWhatsAppProvider:
    p = MockWhatsAppProvider(FrozenClock(BRIEF_EVENING))
    p.given_group("SI Team", SI_TEAM, members=12)
    p.given_group("Management", MANAGEMENT, members=5)
    return p


class TestSatisfiesThePort:
    def test_is_a_whatsapp_provider(self, provider: MockWhatsAppProvider) -> None:
        assert isinstance(provider, WhatsAppProvider)


class TestSending:
    def test_successful_send_is_recorded(self, provider: MockWhatsAppProvider) -> None:
        outcome = provider.send_text(
            external_jid=SI_TEAM, body="*End of Day*", client_message_id="m-1"
        )
        assert outcome.accepted
        assert outcome.ack.is_delivered
        assert provider.delivered_count(SI_TEAM) == 1
        assert provider.sent[0].body == "*End of Day*"

    def test_last_successful_send_is_tracked(
        self, provider: MockWhatsAppProvider
    ) -> None:
        assert provider.health().last_successful_send_at is None
        provider.send_text(external_jid=SI_TEAM, body="x", client_message_id="m-1")
        assert provider.health().last_successful_send_at == BRIEF_EVENING


class TestIdempotency:
    """Scenario 6: a double-click must not produce two messages."""

    def test_replaying_a_message_id_does_not_resend(
        self, provider: MockWhatsAppProvider
    ) -> None:
        first = provider.send_text(
            external_jid=SI_TEAM, body="report", client_message_id="m-1"
        )
        second = provider.send_text(
            external_jid=SI_TEAM, body="report", client_message_id="m-1"
        )

        assert provider.send_attempts == 2
        assert provider.delivered_count(SI_TEAM) == 1, "sent twice"
        assert second.deduplicated
        assert not first.deduplicated
        assert second.provider_message_id == first.provider_message_id

    def test_different_ids_do_send_twice(
        self, provider: MockWhatsAppProvider
    ) -> None:
        """Deduplication must key on the id, not on the body."""
        provider.send_text(external_jid=SI_TEAM, body="same", client_message_id="m-1")
        provider.send_text(external_jid=SI_TEAM, body="same", client_message_id="m-2")
        assert provider.delivered_count(SI_TEAM) == 2

    def test_permanent_failure_is_not_retried_into_success(
        self, provider: MockWhatsAppProvider
    ) -> None:
        """A permanently failed id must keep failing, not succeed on retry."""
        provider.given_failure(SI_TEAM, error_class=ErrorClass.PERMANENT)
        first = provider.send_text(
            external_jid=SI_TEAM, body="x", client_message_id="m-1"
        )
        provider.clear_failures()
        second = provider.send_text(
            external_jid=SI_TEAM, body="x", client_message_id="m-1"
        )

        assert not first.accepted
        assert not second.accepted
        assert provider.delivered_count(SI_TEAM) == 0


class TestFailures:
    def test_permanent_failure_is_not_retryable(
        self, provider: MockWhatsAppProvider
    ) -> None:
        provider.given_failure(
            SI_TEAM, code="GROUP_NOT_FOUND", error_class=ErrorClass.PERMANENT
        )
        outcome = provider.send_text(
            external_jid=SI_TEAM, body="x", client_message_id="m-1"
        )
        assert not outcome.accepted
        assert not outcome.retryable
        assert outcome.error_code == "GROUP_NOT_FOUND"

    def test_transient_failure_is_retryable(
        self, provider: MockWhatsAppProvider
    ) -> None:
        provider.given_failure(SI_TEAM, error_class=ErrorClass.TRANSIENT)
        outcome = provider.send_text(
            external_jid=SI_TEAM, body="x", client_message_id="m-1"
        )
        assert outcome.retryable

    def test_failure_can_be_exhausted_to_exercise_the_retry_ladder(
        self, provider: MockWhatsAppProvider
    ) -> None:
        """Fail twice, then succeed -- the common transient-recovery shape."""
        provider.given_failure(SI_TEAM, error_class=ErrorClass.TRANSIENT, times=2)

        assert not provider.send_text(
            external_jid=SI_TEAM, body="x", client_message_id="a"
        ).accepted
        assert not provider.send_text(
            external_jid=SI_TEAM, body="x", client_message_id="b"
        ).accepted
        assert provider.send_text(
            external_jid=SI_TEAM, body="x", client_message_id="c"
        ).accepted
        assert provider.delivered_count(SI_TEAM) == 1

    def test_scenario_8_one_group_fails_others_succeed(
        self, provider: MockWhatsAppProvider
    ) -> None:
        """A failure for one group must not affect another."""
        ai_team = "120363000000000003@g.us"
        provider.given_group("AI Engineering", ai_team)
        provider.given_failure(ai_team, error_class=ErrorClass.PERMANENT)

        results = {
            jid: provider.send_text(
                external_jid=jid, body="report", client_message_id=f"m-{jid}"
            ).accepted
            for jid in (SI_TEAM, MANAGEMENT, ai_team)
        }

        assert results == {SI_TEAM: True, MANAGEMENT: True, ai_team: False}
        assert provider.delivered_count(SI_TEAM) == 1
        assert provider.delivered_count(MANAGEMENT) == 1
        assert provider.delivered_count(ai_team) == 0


class TestConnectionState:
    def test_disconnected_provider_fails_transiently(
        self, provider: MockWhatsAppProvider
    ) -> None:
        """Scenario 9: WhatsApp down must be a queueable failure, not permanent."""
        provider.given_connection(ConnectionState.LOGIN_REQUIRED)
        outcome = provider.send_text(
            external_jid=SI_TEAM, body="x", client_message_id="m-1"
        )
        assert not outcome.accepted
        assert outcome.retryable

    def test_a_send_refused_while_offline_is_attempted_again_on_reconnect(
        self, provider: MockWhatsAppProvider
    ) -> None:
        """The offline refusal must not be cached as the id's final outcome."""
        provider.given_connection(ConnectionState.UNAVAILABLE)
        provider.send_text(external_jid=SI_TEAM, body="x", client_message_id="m-1")

        provider.given_connection(ConnectionState.CONNECTED)
        retry = provider.send_text(
            external_jid=SI_TEAM, body="x", client_message_id="m-1"
        )

        assert retry.accepted
        assert not retry.deduplicated
        assert provider.delivered_count(SI_TEAM) == 1

    def test_health_reports_state(self, provider: MockWhatsAppProvider) -> None:
        provider.given_connection(ConnectionState.AUTOMATION_ERROR)
        status = provider.health()
        assert status.state is ConnectionState.AUTOMATION_ERROR
        assert not status.can_send


class TestGroupResolution:
    def test_exact_match_ranks_highest(self, provider: MockWhatsAppProvider) -> None:
        candidates = provider.resolve_group("SI Team")
        assert candidates[0].display_name == "SI Team"
        assert candidates[0].confidence == 1.0

    def test_ambiguous_name_returns_every_candidate(
        self, provider: MockWhatsAppProvider
    ) -> None:
        """The brief's example. Picking one automatically could leak an internal
        report into a customer group, so all candidates come back."""
        provider.given_group("SI Team - Bangalore", "120363000000000010@g.us")
        provider.given_group("SI Team - Internal", "120363000000000011@g.us")

        candidates = provider.resolve_group("SI Team")
        assert len(candidates) == 3
        assert candidates[0].display_name == "SI Team"

    def test_no_match_returns_empty(self, provider: MockWhatsAppProvider) -> None:
        assert provider.resolve_group("Nonexistent Group") == ()

    def test_blank_name_returns_empty(self, provider: MockWhatsAppProvider) -> None:
        assert provider.resolve_group("   ") == ()

    def test_matching_is_case_insensitive(
        self, provider: MockWhatsAppProvider
    ) -> None:
        assert provider.resolve_group("si team")[0].external_jid == SI_TEAM


class TestDeliveryLookup:
    def test_ambiguous_timeout_resolves_without_resending(
        self, provider: MockWhatsAppProvider
    ) -> None:
        """The case that produces duplicates in systems that get it wrong: the
        send succeeded but the caller never saw the response."""
        provider.send_text(external_jid=SI_TEAM, body="x", client_message_id="m-1")

        recovered = provider.delivery_state("m-1")
        assert recovered is not None
        assert recovered.accepted
        assert provider.delivered_count(SI_TEAM) == 1

    def test_unknown_id_has_no_delivery_state(
        self, provider: MockWhatsAppProvider
    ) -> None:
        assert provider.delivery_state("never-sent") is None


class TestClockIsInjected:
    def test_send_time_follows_the_injected_clock(self) -> None:
        clock = FrozenClock(BRIEF_EVENING)
        provider = MockWhatsAppProvider(clock)
        provider.given_group("SI Team", SI_TEAM)

        provider.send_text(external_jid=SI_TEAM, body="x", client_message_id="m-1")
        clock.advance(dt.timedelta(hours=4))
        provider.send_text(external_jid=SI_TEAM, body="y", client_message_id="m-2")

        assert provider.sent[1].at - provider.sent[0].at == dt.timedelta(hours=4)
