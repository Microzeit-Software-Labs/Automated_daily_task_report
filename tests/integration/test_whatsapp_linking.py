"""Linking a phone, and reporting who is linked and why a link is unusable --
the Python side of the agent contract, run against the agent's own SQL.

Reuses the FakeAgent from test_local_agent_provider.py, which executes the
exact statements in apps/agent/sql/agent_queries.sql, so these prove the
contract the real agent satisfies rather than a hand-copy of it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from interlock.adapters.persistence.models import WhatsAppAgentStatusRow
from interlock.adapters.persistence.models.whatsapp_agent import STATUS_ROW_ID
from interlock.domain.common.errors import ProviderUnavailableError
from interlock.domain.ports.whatsapp import ConnectionState, PairingState
from tests.integration.test_local_agent_provider import (
    _SQL_PATH,
    FakeAgent,
    _load_named_queries,
    _make_provider,
)

pytestmark = pytest.mark.integration

JID = "919876543210:12@s.whatsapp.net"


def _upsert_status(
    session_factory: sessionmaker[Session],
    *,
    state: str,
    reason: str | None = None,
    account_jid: str | None = None,
    account_name: str | None = None,
    detail: str = "heartbeat",
) -> None:
    """The agent's heartbeat, through the same shared SQL the real agent runs."""
    session = session_factory()
    try:
        session.execute(
            text(_load_named_queries(_SQL_PATH)["upsert_status"]),
            {
                "state": state,
                "detail": detail,
                "status_reason": reason,
                "agent_version": "test",
                "account_jid": account_jid,
                "account_name": account_name,
                "last_successful_send_at": None,
                "last_canary_at": None,
                "last_canary_ok": None,
            },
        )
        session.commit()
    finally:
        session.close()


def _insert_old_status(session_factory: sessionmaker[Session], *, minutes: int = 10) -> None:
    session = session_factory()
    try:
        session.add(
            WhatsAppAgentStatusRow(
                id=STATUS_ROW_ID,
                state="CONNECTED",
                detail="",
                updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes),
            )
        )
        session.commit()
    finally:
        session.close()


class TestHealthReportsWhoIsLinkedAndWhyNot:
    def test_account_and_reason_come_through(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        _upsert_status(
            agent_session_factory,
            state="LOGIN_REQUIRED",
            reason="LOGGED_OUT",
            account_jid=JID,
            account_name="Kaif",
            detail="WhatsApp logged this device out.",
        )

        status = _make_provider(agent_session_factory).health()

        assert status.state is ConnectionState.LOGIN_REQUIRED
        assert status.reason == "LOGGED_OUT"
        assert status.account_jid == JID
        assert status.account_name == "Kaif"
        assert not status.can_send

    def test_the_account_is_kept_when_a_later_heartbeat_has_none(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        """ "Last connected as ..." must survive the disconnect that follows."""
        _upsert_status(agent_session_factory, state="CONNECTED", account_jid=JID)
        _upsert_status(
            agent_session_factory, state="LOGIN_REQUIRED", reason="LOGGED_OUT", account_jid=None
        )

        assert _make_provider(agent_session_factory).health().account_jid == JID

    def test_the_reason_clears_once_healthy_again(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        _upsert_status(agent_session_factory, state="LOGIN_REQUIRED", reason="LOGGED_OUT")
        _upsert_status(agent_session_factory, state="CONNECTED", reason=None)

        assert _make_provider(agent_session_factory).health().reason is None

    def test_a_dead_agent_is_offline_not_a_relink(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        _insert_old_status(agent_session_factory)

        assert _make_provider(agent_session_factory).health().reason == "AGENT_OFFLINE"

    def test_no_agent_ever_is_offline_too(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        assert _make_provider(agent_session_factory).health().reason == "AGENT_OFFLINE"


class TestLinking:
    def test_start_shows_the_qr_the_agent_published(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        _upsert_status(agent_session_factory, state="LOGIN_REQUIRED", reason="NOT_LINKED")
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=2))

        with FakeAgent(agent_session_factory) as agent:
            status = provider.start_link()

        assert agent.control_ops == ["link_start"]
        assert status.state is PairingState.WAITING_FOR_SCAN
        assert status.qr == "qr-payload-1"
        assert status.qr_at is not None
        assert status.pairing_id is not None
        assert status.agent_online

    def test_cancel_clears_the_attempt(self, agent_session_factory: sessionmaker[Session]) -> None:
        _upsert_status(agent_session_factory, state="CONNECTED")
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=2))

        with FakeAgent(agent_session_factory) as agent:
            provider.start_link()
            status = provider.cancel_link()

        assert agent.control_ops == ["link_start", "link_cancel"]
        assert status.state is PairingState.CANCELLED
        assert status.qr is None

    def test_link_status_is_idle_before_anything_starts(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        _upsert_status(agent_session_factory, state="CONNECTED")

        status = _make_provider(agent_session_factory).link_status()

        assert status.state is PairingState.IDLE
        assert status.agent_online
        assert status.qr is None

    def test_no_agent_means_no_qr_and_a_clear_error(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(agent_session_factory)

        assert provider.link_status().agent_online is False
        with pytest.raises(ProviderUnavailableError, match="isn't running"):
            provider.start_link()

    def test_a_stale_agent_counts_as_not_running(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        _insert_old_status(agent_session_factory)

        with pytest.raises(ProviderUnavailableError, match="isn't running"):
            _make_provider(agent_session_factory).start_link()

    def test_cancel_with_no_agent_changes_nothing_and_queues_nothing(
        self, agent_session_factory: sessionmaker[Session], migrated_engine: Engine
    ) -> None:
        status = _make_provider(agent_session_factory).cancel_link()

        assert status.agent_online is False
        with migrated_engine.connect() as conn:
            queued = conn.execute(text("SELECT count(*) FROM whatsapp_agent_commands")).scalar()
        assert queued == 0

    def test_an_agent_that_never_answers_times_out_clearly(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        _upsert_status(agent_session_factory, state="CONNECTED")
        provider = _make_provider(
            agent_session_factory, command_timeout=dt.timedelta(milliseconds=150)
        )

        with pytest.raises(ProviderUnavailableError, match="didn't answer"):
            provider.start_link()  # alive heartbeat, but nothing claims the command


class TestReconnect:
    def test_asks_the_agent_to_reconnect(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        _upsert_status(agent_session_factory, state="UNAVAILABLE", reason="REPLACED")
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=2))

        with FakeAgent(agent_session_factory) as agent:
            provider.reconnect()

        assert agent.control_ops == ["reconnect"]

    def test_a_link_that_does_not_exist_cannot_be_reconnected(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        _upsert_status(agent_session_factory, state="LOGIN_REQUIRED", reason="NOT_LINKED")
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=2))

        with FakeAgent(agent_session_factory) as agent:
            agent.reconnect_ok = False
            with pytest.raises(ProviderUnavailableError, match="Link one with a QR code"):
                provider.reconnect()


class TestResolveGroupWhenTheAgentCannotLookUp:
    def test_the_agents_error_becomes_a_clear_error_not_a_timeout(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=2))

        with FakeAgent(agent_session_factory) as agent:
            agent.resolve_error = {"error": "NOT_AVAILABLE", "detail": "WhatsApp is not connected"}
            with pytest.raises(ProviderUnavailableError, match="WhatsApp is not connected"):
                provider.resolve_group("Anything")


class TestControlOnlyClaim:
    """While the agent is not connected it claims only link-management
    commands (``claim_next_control``), so a send or a lookup it cannot do is
    never claimed and left stuck."""

    @staticmethod
    def _claim_control(session_factory: sessionmaker[Session]) -> list[str]:
        session = session_factory()
        try:
            rows = (
                session.execute(
                    text(_load_named_queries(_SQL_PATH)["claim_next_control"]),
                    {"claimed_by": "t", "margin_seconds": 0},
                )
                .mappings()
                .all()
            )
            session.commit()
            return [row["op"] for row in rows]
        finally:
            session.close()

    def test_claims_the_control_command_and_leaves_the_send_pending(
        self, agent_session_factory: sessionmaker[Session], migrated_engine: Engine
    ) -> None:
        provider = _make_provider(
            agent_session_factory, command_timeout=dt.timedelta(milliseconds=100)
        )
        provider.send_text(external_jid="a@g.us", body="x", client_message_id="m-1")
        now = dt.datetime.now(dt.UTC)
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO whatsapp_agent_commands "
                    "(id, op, payload, status, created_at, expires_at) "
                    "VALUES (:id, 'reconnect', '{}'::jsonb, 'PENDING', :now, :exp)"
                ),
                {
                    "id": "01CONTROLCOMMAND000000000A",
                    "now": now,
                    "exp": now + dt.timedelta(seconds=30),
                },
            )

        assert self._claim_control(agent_session_factory) == ["reconnect"]

        with migrated_engine.connect() as conn:
            states = dict(
                conn.execute(text("SELECT op, status FROM whatsapp_agent_commands")).all()
            )
        assert states == {"send_text": "PENDING", "reconnect": "CLAIMED"}

    def test_claims_nothing_when_only_non_control_commands_wait(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(
            agent_session_factory, command_timeout=dt.timedelta(milliseconds=100)
        )
        provider.send_text(external_jid="a@g.us", body="x", client_message_id="m-1")

        assert self._claim_control(agent_session_factory) == []
