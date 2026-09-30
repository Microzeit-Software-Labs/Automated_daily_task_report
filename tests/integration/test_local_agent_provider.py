"""LocalAgentProvider against a real database and a FakeAgent standing in for
the Node process -- mirroring test_mock_provider.py's pinned behaviours at
this heavier layer, since this design introduces genuine timing/asynchrony
MockWhatsAppProvider never had.

The FakeAgent runs the *exact* statements in apps/agent/sql/agent_queries.sql
(loaded and executed here, not hand-copied), so a test proving Python's side
of the contract holds is proving it against the production SQL.

Needs committed data -- the provider and the FakeAgent use separate
connections/sessions, so tests/integration/conftest.py's rolled-back
db_session fixture (which nothing outside its own transaction can see) does
not work here. Uses the real, session-scoped migrated_engine instead, with
the two agent tables truncated per test.

Uses a real SystemClock, not FrozenClock: expiry and heartbeat staleness are
computed against Postgres's own now() (see local_agent.py's module
docstring), so Python's clock has to be real wall time too, or an arbitrary
frozen instant would already be "expired" relative to the database.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from interlock.adapters.persistence.base import make_engine, make_session_factory
from interlock.adapters.persistence.models import WhatsAppAgentStatusRow
from interlock.adapters.persistence.models.whatsapp_agent import STATUS_ROW_ID
from interlock.adapters.whatsapp.local_agent import LocalAgentProvider
from interlock.domain.common.clock import SystemClock
from interlock.domain.common.errors import ProviderUnavailableError
from interlock.domain.ports.whatsapp import ConnectionState, ErrorClass

pytestmark = pytest.mark.integration

_SQL_PATH = (
    Path(__file__).resolve().parent.parent.parent / "apps" / "agent" / "sql" / "agent_queries.sql"
)


def _load_named_queries(path: Path) -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^-- name:\s*(\w+)\s*$", line)
        if match:
            current = match.group(1)
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in blocks.items()}


@dataclasses.dataclass(frozen=True, slots=True)
class FakeAgentBehavior:
    """What the fake agent does the next time it claims a send_text for one
    external_jid."""

    outcome: str
    """"success" | "permanent" | "transient" | "hang"."""
    error_code: str = "GROUP_NOT_FOUND"
    error_detail: str = ""


class FakeAgent:
    """A background thread claiming and completing rows exactly the way the
    real Node agent would, via the shared SQL contract. Configured per test
    through ``send_behaviors`` (keyed by external_jid) and
    ``group_candidates`` (keyed by the name passed to resolve_group)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        poll_interval: dt.timedelta = dt.timedelta(milliseconds=20),
    ) -> None:
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        self._queries = _load_named_queries(_SQL_PATH)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._mint_counter = 0

        self.send_behaviors: dict[str, FakeAgentBehavior] = {}
        self.group_candidates: dict[str, list[dict[str, Any]]] = {}
        self.resolve_error: dict[str, Any] | None = None
        """When set, resolve_group is answered with this error result."""
        self.reconnect_ok = True
        """What a ``reconnect`` answers: ok, or "not linked"."""
        self.control_ops: list[str] = []
        self.sent_count = 0
        self.wa_message_ids_minted: list[str] = []

    def __enter__(self) -> FakeAgent:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self._claim_and_process_one():
                time.sleep(self._poll_interval.total_seconds())

    def _claim_and_process_one(self) -> bool:
        session = self._session_factory()
        try:
            row = (
                session.execute(
                    text(self._queries["claim_next"]),
                    {"claimed_by": "fake-agent", "margin_seconds": 0},
                )
                .mappings()
                .first()
            )
            session.commit()
        finally:
            session.close()
        if row is None:
            return False
        self._process(dict(row))
        return True

    def _process(self, row: dict[str, Any]) -> None:
        if row["op"] == "send_text":
            self._process_send_text(row)
        elif row["op"] == "resolve_group":
            self._process_resolve_group(row)
        elif row["op"] == "link_start":
            self.control_ops.append("link_start")
            self._update_pairing(
                state="WAITING_FOR_SCAN",
                pairing_id=row["id"],
                qr="qr-payload-1",
                detail="Scan the code with WhatsApp.",
            )
            self._complete(row["id"], result={"started": True})
        elif row["op"] == "link_cancel":
            self.control_ops.append("link_cancel")
            self._update_pairing(
                state="CANCELLED", pairing_id=None, qr=None, detail="Linking was cancelled."
            )
            self._complete(row["id"], result={"cancelled": True})
        elif row["op"] == "reconnect":
            self.control_ops.append("reconnect")
            self._complete(
                row["id"],
                result={
                    "ok": self.reconnect_ok,
                    "outcome": "started" if self.reconnect_ok else "not_linked",
                },
            )
        else:
            self._complete(
                row["id"],
                result={
                    "accepted": False,
                    "error_code": "UNKNOWN_OP",
                    "error_detail": row["op"],
                    "error_class": "PERMANENT",
                },
            )

    def _process_send_text(self, row: dict[str, Any]) -> None:
        jid = row["payload"]["external_jid"]
        behavior = self.send_behaviors.get(jid, FakeAgentBehavior(outcome="success"))

        if behavior.outcome == "hang":
            return  # left CLAIMED -- simulates an agent stuck mid-send

        if behavior.outcome in ("success", "transient"):
            wa_id = row["wa_message_id"] or self._mint_wa_id()
            if row["wa_message_id"] is None:
                self._set_wa_message_id(row["id"], wa_id)
            if behavior.outcome == "transient":
                self._release_transient(row["id"])
                return
            with self._lock:
                self.sent_count += 1
            self._complete(
                row["id"],
                result={"accepted": True, "provider_message_id": wa_id, "ack": "SERVER"},
            )
            return

        if behavior.outcome == "permanent":
            self._complete(
                row["id"],
                result={
                    "accepted": False,
                    "error_code": behavior.error_code,
                    "error_detail": behavior.error_detail,
                    "error_class": "PERMANENT",
                },
            )
            return

        raise ValueError(f"unknown FakeAgentBehavior.outcome {behavior.outcome!r}")

    def _process_resolve_group(self, row: dict[str, Any]) -> None:
        if self.resolve_error is not None:
            self._complete(row["id"], result=self.resolve_error)
            return
        name = row["payload"]["name"]
        candidates = self.group_candidates.get(name)
        if candidates is None:
            return  # left CLAIMED -- simulates the agent never answering
        self._complete(row["id"], result={"candidates": candidates})

    def _update_pairing(
        self, *, state: str, pairing_id: str | None, qr: str | None, detail: str
    ) -> None:
        """What the real agent does as a phone links -- via the shared SQL."""
        session = self._session_factory()
        try:
            session.execute(
                text(self._queries["update_pairing"]),
                {
                    "pairing_state": state,
                    "pairing_id": pairing_id,
                    "pairing_qr": qr,
                    "pairing_qr_at": dt.datetime.now(dt.UTC) if qr else None,
                    "pairing_detail": detail,
                },
            )
            session.commit()
        finally:
            session.close()

    def _mint_wa_id(self) -> str:
        with self._lock:
            self._mint_counter += 1
            wa_id = f"fake-wa-{self._mint_counter:06d}"
        self.wa_message_ids_minted.append(wa_id)
        return wa_id

    def _complete(self, command_id: str, *, result: dict[str, Any]) -> None:
        session = self._session_factory()
        try:
            session.execute(
                text(self._queries["complete"]), {"id": command_id, "result": json.dumps(result)}
            )
            session.commit()
        finally:
            session.close()

    def _release_transient(self, command_id: str) -> None:
        session = self._session_factory()
        try:
            session.execute(text(self._queries["release_transient"]), {"id": command_id})
            session.commit()
        finally:
            session.close()

    def _set_wa_message_id(self, command_id: str, wa_message_id: str) -> None:
        session = self._session_factory()
        try:
            session.execute(
                text(self._queries["set_wa_message_id"]),
                {"id": command_id, "wa_message_id": wa_message_id},
            )
            session.commit()
        finally:
            session.close()


def _make_provider(
    session_factory: sessionmaker[Session],
    *,
    command_timeout: dt.timedelta = dt.timedelta(milliseconds=300),
    heartbeat_stale_after: dt.timedelta = dt.timedelta(seconds=90),
) -> LocalAgentProvider:
    return LocalAgentProvider(
        session_factory=session_factory,
        clock=SystemClock(),
        poll_interval=dt.timedelta(milliseconds=20),
        command_timeout=command_timeout,
        heartbeat_stale_after=heartbeat_stale_after,
    )


class TestIdempotency:
    def test_replaying_a_message_id_does_not_resend(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(agent_session_factory)
        jid = "si@g.us"

        with FakeAgent(agent_session_factory) as agent:
            agent.send_behaviors[jid] = FakeAgentBehavior(outcome="success")
            first = provider.send_text(external_jid=jid, body="report", client_message_id="m-1")
            second = provider.send_text(external_jid=jid, body="report", client_message_id="m-1")

        assert first.accepted
        assert not first.deduplicated
        assert second.accepted
        assert second.deduplicated
        assert second.provider_message_id == first.provider_message_id
        assert agent.sent_count == 1

    def test_permanent_failure_is_memoised(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(agent_session_factory)
        jid = "permanent@g.us"

        with FakeAgent(agent_session_factory) as agent:
            agent.send_behaviors[jid] = FakeAgentBehavior(
                outcome="permanent", error_code="GROUP_NOT_FOUND"
            )
            first = provider.send_text(external_jid=jid, body="x", client_message_id="m-1")
            second = provider.send_text(external_jid=jid, body="x", client_message_id="m-1")

        assert not first.accepted
        assert not first.deduplicated
        assert first.error_code == "GROUP_NOT_FOUND"
        assert not second.accepted
        assert second.deduplicated
        assert second.error_code == "GROUP_NOT_FOUND"
        assert agent.sent_count == 0


class TestNoAgentOrAHungAgent:
    def test_a_hung_claim_returns_transient_never_ambiguous(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=1))
        jid = "hang@g.us"

        with FakeAgent(agent_session_factory) as agent:
            agent.send_behaviors[jid] = FakeAgentBehavior(outcome="hang")
            outcome = provider.send_text(external_jid=jid, body="x", client_message_id="m-1")

        assert not outcome.accepted
        assert outcome.error_class is ErrorClass.TRANSIENT

    def test_offline_then_online_sends_exactly_once(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        """No fake agent at all for the first attempt -- mirrors
        MockWhatsAppProvider's own pinned test: the offline refusal must not
        be cached as the id's final outcome."""
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=1))
        jid = "reconnect@g.us"

        first = provider.send_text(external_jid=jid, body="x", client_message_id="m-1")
        assert not first.accepted
        assert first.error_class is ErrorClass.TRANSIENT

        with FakeAgent(agent_session_factory) as agent:
            agent.send_behaviors[jid] = FakeAgentBehavior(outcome="success")
            retry = provider.send_text(external_jid=jid, body="x", client_message_id="m-1")

        assert retry.accepted
        assert not retry.deduplicated
        assert agent.sent_count == 1

    def test_python_gives_up_then_the_agent_arrives_too_late_never_sends(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        """The row expires with real, elapsed wall-clock time -- a late
        agent must not send it after Python has already reported it FAILED
        up the stack."""
        provider = _make_provider(
            agent_session_factory, command_timeout=dt.timedelta(milliseconds=150)
        )
        jid = "orphan@g.us"

        outcome = provider.send_text(external_jid=jid, body="x", client_message_id="m-1")
        assert not outcome.accepted

        time.sleep(0.3)  # real time past expiry, agent still not running

        with FakeAgent(agent_session_factory) as agent:
            agent.send_behaviors[jid] = FakeAgentBehavior(outcome="success")
            time.sleep(0.3)  # ample chance to (wrongly) claim the expired row
            assert agent.sent_count == 0


class TestTransientRelease:
    def test_resent_with_the_same_wa_message_id(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=1))
        jid = "transient@g.us"

        with FakeAgent(agent_session_factory) as agent:
            agent.send_behaviors[jid] = FakeAgentBehavior(outcome="transient")
            first = provider.send_text(external_jid=jid, body="x", client_message_id="m-1")
            assert not first.accepted
            assert first.error_class is ErrorClass.TRANSIENT
            assert len(agent.wa_message_ids_minted) == 1

            agent.send_behaviors[jid] = FakeAgentBehavior(outcome="success")
            second = provider.send_text(external_jid=jid, body="x", client_message_id="m-1")

        assert second.accepted
        assert not second.deduplicated
        assert second.provider_message_id == agent.wa_message_ids_minted[0]
        assert len(agent.wa_message_ids_minted) == 1
        assert agent.sent_count == 1


class TestResolveGroup:
    def test_returns_the_agents_candidates(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=1))
        with FakeAgent(agent_session_factory) as agent:
            agent.group_candidates["SI Team"] = [
                {
                    "external_jid": "si@g.us",
                    "display_name": "SI Team",
                    "member_count": 12,
                    "confidence": 1.0,
                }
            ]
            candidates = provider.resolve_group("SI Team")

        assert len(candidates) == 1
        assert candidates[0].external_jid == "si@g.us"
        assert candidates[0].member_count == 12

    def test_timeout_raises_provider_unavailable_not_an_empty_result(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        """No error channel on the return type -- "the agent didn't answer"
        must stay distinguishable from "genuinely no matches"."""
        provider = _make_provider(
            agent_session_factory, command_timeout=dt.timedelta(milliseconds=150)
        )
        with pytest.raises(ProviderUnavailableError):
            provider.resolve_group("Nobody Configured This Name")


class TestDeliveryState:
    def test_resolves_a_completed_send_without_resending(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(agent_session_factory, command_timeout=dt.timedelta(seconds=1))
        jid = "lookup@g.us"

        with FakeAgent(agent_session_factory) as agent:
            agent.send_behaviors[jid] = FakeAgentBehavior(outcome="success")
            provider.send_text(external_jid=jid, body="x", client_message_id="m-1")
            resolved = provider.delivery_state("m-1")

        assert resolved is not None
        assert resolved.accepted
        assert agent.sent_count == 1

    def test_none_for_a_still_pending_command(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        provider = _make_provider(
            agent_session_factory, command_timeout=dt.timedelta(milliseconds=100)
        )
        provider.send_text(external_jid="pending@g.us", body="x", client_message_id="m-1")
        assert provider.delivery_state("m-1") is None

    def test_none_for_an_unknown_id(self, agent_session_factory: sessionmaker[Session]) -> None:
        provider = _make_provider(agent_session_factory)
        assert provider.delivery_state("never-sent") is None


class TestHealth:
    def test_missing_status_row_is_unavailable(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        status = _make_provider(agent_session_factory).health()
        assert status.state is ConnectionState.UNAVAILABLE

    def test_a_fresh_heartbeat_is_reported_as_is(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        session = agent_session_factory()
        try:
            session.add(
                WhatsAppAgentStatusRow(
                    id=STATUS_ROW_ID, state="CONNECTED", detail="ok", updated_at=now
                )
            )
            session.commit()
        finally:
            session.close()

        status = _make_provider(
            agent_session_factory, heartbeat_stale_after=dt.timedelta(seconds=10)
        ).health()
        assert status.state is ConnectionState.CONNECTED
        assert status.can_send

    def test_a_stale_heartbeat_is_unavailable_regardless_of_recorded_state(
        self, agent_session_factory: sessionmaker[Session]
    ) -> None:
        old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30)
        session = agent_session_factory()
        try:
            session.add(
                WhatsAppAgentStatusRow(
                    id=STATUS_ROW_ID, state="CONNECTED", detail="ok", updated_at=old
                )
            )
            session.commit()
        finally:
            session.close()

        status = _make_provider(
            agent_session_factory, heartbeat_stale_after=dt.timedelta(seconds=5)
        ).health()
        assert status.state is ConnectionState.UNAVAILABLE

    def test_never_raises_when_the_database_is_unreachable(self) -> None:
        # connect_args sets a short connect_timeout: a closed port on this
        # machine can otherwise take Windows' full TCP retry timeout (~130s)
        # to report connection failure, rather than refusing instantly.
        bad_engine = make_engine(
            "postgresql+psycopg://baduser:badpass@127.0.0.1:59999/nonexistent",
            connect_args={"connect_timeout": 2},
        )
        provider = _make_provider(make_session_factory(bad_engine))

        status = provider.health()

        assert status.state is ConnectionState.UNAVAILABLE
        assert "could not reach" in status.detail
