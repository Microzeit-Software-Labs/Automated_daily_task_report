"""WhatsAppAgentRepository against a real database: the Python side of the
outbox. See apps/agent/sql/agent_queries.sql for the agent's own side of the
same two tables.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import WhatsAppAgentStatusRow
from interlock.adapters.persistence.models.whatsapp_agent import STATUS_ROW_ID
from interlock.adapters.persistence.whatsapp_agent_repository import WhatsAppAgentRepository
from interlock.domain.common.ids import new_id
from tests.conftest import BRIEF_EVENING

T0 = BRIEF_EVENING
EXPIRES = T0 + dt.timedelta(seconds=20)


def _insert_status(session: Session, *, state: str, updated_at: dt.datetime) -> None:
    session.add(
        WhatsAppAgentStatusRow(id=STATUS_ROW_ID, state=state, detail="", updated_at=updated_at)
    )
    session.flush()


class TestEnqueue:
    def test_creates_a_pending_row(self, db_session: Session) -> None:
        repo = WhatsAppAgentRepository(db_session)
        command_id = new_id()

        repo.enqueue(
            command_id=command_id,
            op="send_text",
            payload={"external_jid": "a@g.us", "body": "x"},
            client_message_id="m-1",
            now=T0,
            expires_at=EXPIRES,
        )

        record = repo.get_by_id(command_id)
        assert record is not None
        assert record.status == "PENDING"
        assert record.client_message_id == "m-1"
        assert record.op == "send_text"

    def test_a_second_enqueue_with_the_same_client_message_id_refreshes_expiry(
        self, db_session: Session
    ) -> None:
        """Not a new row -- the whole point of the partial unique index."""
        repo = WhatsAppAgentRepository(db_session)
        first_id = new_id()
        repo.enqueue(
            command_id=first_id,
            op="send_text",
            payload={},
            client_message_id="m-1",
            now=T0,
            expires_at=EXPIRES,
        )

        second_id = new_id()
        later_expiry = T0 + dt.timedelta(seconds=25)
        repo.enqueue(
            command_id=second_id,
            op="send_text",
            payload={},
            client_message_id="m-1",
            now=T0 + dt.timedelta(seconds=5),
            expires_at=later_expiry,
        )

        record = repo.get_by_id(first_id)
        assert record is not None
        assert record.expires_at == later_expiry
        assert repo.get_by_id(second_id) is None

    def test_does_not_refresh_a_row_that_is_no_longer_pending(
        self, db_session: Session
    ) -> None:
        repo = WhatsAppAgentRepository(db_session)
        command_id = new_id()
        repo.enqueue(
            command_id=command_id,
            op="send_text",
            payload={},
            client_message_id="m-1",
            now=T0,
            expires_at=EXPIRES,
        )
        db_session.execute(
            text("UPDATE whatsapp_agent_commands SET status = 'DONE' WHERE id = :id"),
            {"id": command_id},
        )
        db_session.flush()

        repo.enqueue(
            command_id=new_id(),
            op="send_text",
            payload={},
            client_message_id="m-1",
            now=T0 + dt.timedelta(seconds=5),
            expires_at=T0 + dt.timedelta(seconds=999),
        )

        record = repo.get_by_id(command_id)
        assert record is not None
        assert record.expires_at == EXPIRES

    def test_resolve_group_rows_never_conflict_on_a_missing_client_message_id(
        self, db_session: Session
    ) -> None:
        repo = WhatsAppAgentRepository(db_session)
        id_a, id_b = new_id(), new_id()
        for command_id in (id_a, id_b):
            repo.enqueue(
                command_id=command_id,
                op="resolve_group",
                payload={"name": "SI Team"},
                client_message_id=None,
                now=T0,
                expires_at=EXPIRES,
            )

        assert repo.get_by_id(id_a) is not None
        assert repo.get_by_id(id_b) is not None


class TestGetDoneByClientMessageId:
    def test_none_while_pending(self, db_session: Session) -> None:
        repo = WhatsAppAgentRepository(db_session)
        repo.enqueue(
            command_id=new_id(),
            op="send_text",
            payload={},
            client_message_id="m-1",
            now=T0,
            expires_at=EXPIRES,
        )
        assert repo.get_done_by_client_message_id("m-1") is None

    def test_returns_the_row_once_done(self, db_session: Session) -> None:
        repo = WhatsAppAgentRepository(db_session)
        command_id = new_id()
        repo.enqueue(
            command_id=command_id,
            op="send_text",
            payload={},
            client_message_id="m-1",
            now=T0,
            expires_at=EXPIRES,
        )
        db_session.execute(
            text(
                "UPDATE whatsapp_agent_commands SET status = 'DONE', result = CAST(:r AS jsonb) "
                "WHERE id = :id"
            ),
            {"r": '{"accepted": true}', "id": command_id},
        )
        db_session.flush()

        record = repo.get_done_by_client_message_id("m-1")
        assert record is not None
        assert record.result == {"accepted": True}

    def test_none_for_an_unknown_id(self, db_session: Session) -> None:
        repo = WhatsAppAgentRepository(db_session)
        assert repo.get_done_by_client_message_id("never-sent") is None


class TestGetStatusWithStaleness:
    def test_none_when_never_reported(self, db_session: Session) -> None:
        repo = WhatsAppAgentRepository(db_session)
        status, stale = repo.get_status_with_staleness(
            now=T0, stale_after=dt.timedelta(seconds=90)
        )
        assert status is None
        assert stale is True

    def test_a_fresh_row_is_not_stale(self, db_session: Session) -> None:
        _insert_status(db_session, state="CONNECTED", updated_at=T0)
        repo = WhatsAppAgentRepository(db_session)
        status, stale = repo.get_status_with_staleness(
            now=T0 + dt.timedelta(seconds=30), stale_after=dt.timedelta(seconds=90)
        )
        assert status is not None
        assert status.state == "CONNECTED"
        assert stale is False

    def test_an_old_row_is_stale(self, db_session: Session) -> None:
        _insert_status(db_session, state="CONNECTED", updated_at=T0)
        repo = WhatsAppAgentRepository(db_session)
        _, stale = repo.get_status_with_staleness(
            now=T0 + dt.timedelta(seconds=200), stale_after=dt.timedelta(seconds=90)
        )
        assert stale is True


class TestConstraints:
    def test_op_must_be_a_known_value(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError, match="ck_whatsapp_agent_commands_op"):
            db_session.execute(
                text(
                    "INSERT INTO whatsapp_agent_commands "
                    "(id, op, payload, status, created_at, expires_at) "
                    "VALUES (:id, 'bogus', '{}'::jsonb, 'PENDING', :now, :exp)"
                ),
                {"id": new_id(), "now": T0, "exp": EXPIRES},
            )

    def test_status_must_be_a_known_value(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError, match="ck_whatsapp_agent_commands_status"):
            db_session.execute(
                text(
                    "INSERT INTO whatsapp_agent_commands "
                    "(id, op, payload, status, created_at, expires_at) "
                    "VALUES (:id, 'send_text', '{}'::jsonb, 'bogus', :now, :exp)"
                ),
                {"id": new_id(), "now": T0, "exp": EXPIRES},
            )

    def test_two_open_commands_for_the_same_client_message_id_are_rejected(
        self, db_session: Session
    ) -> None:
        """The DB-level backstop underneath enqueue()'s own ON CONFLICT
        handling -- proven here by going around the repository entirely."""
        repo = WhatsAppAgentRepository(db_session)
        repo.enqueue(
            command_id=new_id(),
            op="send_text",
            payload={},
            client_message_id="m-1",
            now=T0,
            expires_at=EXPIRES,
        )
        with pytest.raises(IntegrityError, match="uq_whatsapp_agent_commands_client_message_id"):
            db_session.execute(
                text(
                    "INSERT INTO whatsapp_agent_commands "
                    "(id, op, payload, client_message_id, status, created_at, expires_at) "
                    "VALUES (:id, 'send_text', '{}'::jsonb, 'm-1', 'PENDING', :now, :exp)"
                ),
                {"id": new_id(), "now": T0, "exp": EXPIRES},
            )

    def test_status_state_must_be_a_known_value(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError, match="ck_whatsapp_agent_status_state"):
            db_session.execute(
                text(
                    "INSERT INTO whatsapp_agent_status (id, state, detail, updated_at) "
                    "VALUES ('agent', 'bogus', '', :now)"
                ),
                {"now": T0},
            )
