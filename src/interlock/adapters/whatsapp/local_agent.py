"""The real WhatsApp delivery path: a local Baileys agent reached through a
Postgres outbox rather than a socket.

See ``adapters/persistence/whatsapp_agent_repository.py`` and
``apps/agent/sql/agent_queries.sql`` for the contract this implements against.
Two invariants worth restating here, because they are what make this provider
satisfy the same guarantees :class:`~interlock.adapters.whatsapp.mock.
MockWhatsAppProvider` already does:

* A command row only ever reaches ``DONE`` for a *terminal* outcome. A poll
  timeout is always reported as :class:`ErrorClass.TRANSIENT` here -- never
  ``AMBIGUOUS`` -- because a row this call gave up on is exactly the row a
  later retry (same ``client_message_id``) will re-attach to and eventually
  resolve. This is what lets ``scheduler_service.py`` stay untouched: it
  already only retries ``TRANSIENT``.
* ``send_text`` checks for an existing *``DONE``* result before doing
  anything else, mirroring the mock's own idempotency check exactly (dedup
  before connection state, before anything else). A row that exists but is
  not yet ``DONE`` is not a duplicate to detect -- it is the same in-flight
  attempt this call should simply wait on.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from sqlalchemy.orm import Session, sessionmaker

from interlock.adapters.persistence.base import unit_of_work
from interlock.adapters.persistence.whatsapp_agent_repository import (
    CommandRecord,
    WhatsAppAgentRepository,
)
from interlock.domain.common.clock import Clock
from interlock.domain.common.errors import ProviderUnavailableError
from interlock.domain.common.ids import new_id
from interlock.domain.ports.whatsapp import (
    ConnectionState,
    DeliveryAck,
    ErrorClass,
    GroupCandidate,
    LinkStatus,
    PairingState,
    ProviderStatus,
    SendOutcome,
)

_T = TypeVar("_T")

AGENT_OFFLINE = "AGENT_OFFLINE"
"""``ProviderStatus.reason`` when the agent process isn't running (no recent
heartbeat) -- a different problem from a WhatsApp session that needs relinking."""


class LocalAgentProvider:
    name = "local_agent"

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        clock: Clock,
        poll_interval: dt.timedelta,
        command_timeout: dt.timedelta,
        heartbeat_stale_after: dt.timedelta,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._poll_interval = poll_interval
        self._command_timeout = command_timeout
        self._heartbeat_stale_after = heartbeat_stale_after

    def health(self) -> ProviderStatus:
        """Never raises: ``/readiness`` (Scenario 9) must keep returning a
        clean verdict even when the agent, or the database itself, cannot be
        reached -- a dead WhatsApp session must never look like the whole
        application is down."""
        now = self._clock.now()
        try:
            status, stale = self._with_repo(
                lambda repo: repo.get_status_with_staleness(
                    now=now, stale_after=self._heartbeat_stale_after
                )
            )
        except Exception as exc:  # deliberately blanket -- see the docstring above
            return ProviderStatus(
                state=ConnectionState.UNAVAILABLE,
                checked_at=now,
                provider=self.name,
                detail=f"could not reach the agent status table: {exc}",
                reason=AGENT_OFFLINE,
            )

        if status is None:
            return ProviderStatus(
                state=ConnectionState.UNAVAILABLE,
                checked_at=now,
                provider=self.name,
                detail="local agent has never reported in",
                reason=AGENT_OFFLINE,
            )
        if stale:
            # Regardless of what state the row last recorded -- a dead agent
            # process cannot be trusted to have updated it truthfully on its
            # way out.
            return ProviderStatus(
                state=ConnectionState.UNAVAILABLE,
                checked_at=now,
                provider=self.name,
                last_successful_send_at=status.last_successful_send_at,
                detail=f"agent heartbeat stale (last seen {status.updated_at.isoformat()})",
                reason=AGENT_OFFLINE,
                account_jid=status.account_jid,
                account_name=status.account_name,
            )
        return ProviderStatus(
            state=ConnectionState(status.state),
            checked_at=now,
            provider=self.name,
            last_successful_send_at=status.last_successful_send_at,
            detail=status.detail,
            reason=status.status_reason,
            account_jid=status.account_jid,
            account_name=status.account_name,
        )

    def resolve_group(self, name: str) -> Sequence[GroupCandidate]:
        command_id = new_id()
        now = self._clock.now()
        self._with_repo(
            lambda repo: repo.enqueue(
                command_id=command_id,
                op="resolve_group",
                payload={"name": name},
                client_message_id=None,
                now=now,
                expires_at=now + self._command_timeout,
            )
        )
        record = self._poll_until(lambda: self._get_done_by_id(command_id))
        if record is None:
            # No error channel on this return type -- raising is the only way
            # "the agent didn't answer" stays distinguishable from "genuinely
            # no matches", which a bare () would otherwise hide.
            raise ProviderUnavailableError(
                "The local WhatsApp agent did not respond to resolve_group in time.",
                op="resolve_group",
            )
        return _candidates_from_result(record.result)

    def send_text(
        self,
        *,
        external_jid: str,
        body: str,
        client_message_id: str,
        image_png: bytes | None = None,
    ) -> SendOutcome:
        existing = self._with_repo(
            lambda repo: repo.get_done_by_client_message_id(client_message_id)
        )
        if existing is not None:
            return _outcome_from_record(existing, deduplicated=True)

        now = self._clock.now()
        expires_at = now + self._command_timeout
        self._with_repo(
            lambda repo: repo.enqueue(
                command_id=new_id(),
                op="send_text",
                payload=_send_payload(external_jid, body, image_png),
                client_message_id=client_message_id,
                now=now,
                expires_at=expires_at,
            )
        )

        record = self._poll_until(
            lambda: self._with_repo(
                lambda repo: repo.get_done_by_client_message_id(client_message_id)
            )
        )
        if record is None:
            return SendOutcome.failure(
                client_message_id=client_message_id,
                code="AGENT_TIMEOUT",
                detail=(f"local agent did not complete this send within {self._command_timeout}"),
                error_class=ErrorClass.TRANSIENT,
            )
        return _outcome_from_record(record, deduplicated=False)

    def delivery_state(self, client_message_id: str) -> SendOutcome | None:
        record = self._with_repo(lambda repo: repo.get_done_by_client_message_id(client_message_id))
        return None if record is None else _outcome_from_record(record, deduplicated=False)

    # -- linking a phone (WhatsAppLinking) -------------------------------------
    #
    # The UI never talks to the agent. It enqueues a control command here and
    # reads progress (including the QR code) from the status row the agent keeps
    # up to date -- the same outbox used for sending, nothing new.

    def start_link(self) -> LinkStatus:
        self._require_agent_running()
        self._run_control("link_start")
        return self.link_status()

    def cancel_link(self) -> LinkStatus:
        if not self.link_status().agent_online:
            return self.link_status()  # nothing is running to cancel
        self._run_control("link_cancel")
        return self.link_status()

    def reconnect(self) -> None:
        self._require_agent_running()
        record = self._run_control("reconnect")
        if (record.result or {}).get("ok") is False:
            raise ProviderUnavailableError(
                "WhatsApp isn't linked to a phone yet. Link one with a QR code instead.",
                outcome=(record.result or {}).get("outcome"),
            )

    def link_status(self) -> LinkStatus:
        now = self._clock.now()
        try:
            status, stale = self._with_repo(
                lambda repo: repo.get_status_with_staleness(
                    now=now, stale_after=self._heartbeat_stale_after
                )
            )
        except Exception as exc:  # the UI polls this; it must never blow up
            return LinkStatus(
                state=PairingState.IDLE,
                agent_online=False,
                detail=f"could not reach the agent status table: {exc}",
            )
        if status is None:
            return LinkStatus(state=PairingState.IDLE, agent_online=False)
        return LinkStatus(
            state=PairingState(status.pairing_state),
            detail=status.pairing_detail,
            pairing_id=status.pairing_id,
            qr=status.pairing_qr,
            qr_at=status.pairing_qr_at,
            agent_online=not stale,
        )

    def _require_agent_running(self) -> None:
        if not self.link_status().agent_online:
            raise ProviderUnavailableError(
                "Interlock's WhatsApp service isn't running, so there is nothing to "
                "link yet. Start Interlock and try again.",
                reason=AGENT_OFFLINE,
            )

    def _run_control(self, op: str) -> CommandRecord:
        """Enqueue a link-management command and wait for the agent to accept it.

        These complete as soon as the agent has *started* the work, so the
        wait is short; the long part (a person scanning a QR) is reported
        through the status row instead.
        """
        command_id = new_id()
        now = self._clock.now()
        self._with_repo(
            lambda repo: repo.enqueue(
                command_id=command_id,
                op=op,
                payload={},
                client_message_id=None,
                now=now,
                expires_at=now + self._command_timeout,
            )
        )
        record = self._poll_until(lambda: self._get_done_by_id(command_id))
        if record is None:
            raise ProviderUnavailableError("The WhatsApp service didn't answer in time.", op=op)
        return record

    # -- internals ------------------------------------------------------------

    def _with_repo(self, fn: Callable[[WhatsAppAgentRepository], _T]) -> _T:
        with unit_of_work(self._session_factory) as session:
            return fn(WhatsAppAgentRepository(session))

    def _get_done_by_id(self, command_id: str) -> CommandRecord | None:
        record = self._with_repo(lambda repo: repo.get_by_id(command_id))
        return record if record is not None and record.status == "DONE" else None

    def _poll_until(self, fetch: Callable[[], CommandRecord | None]) -> CommandRecord | None:
        """Poll ``fetch`` (which must return non-``None`` only once the row
        is genuinely ``DONE``) with a fresh session each time -- opening new
        rather than holding one open is what the rest of this codebase
        already does per tick (see ``workers/loop.py``'s docstring), and it
        removes any question of a stale, identity-mapped object masking a
        row the agent has since completed."""
        deadline = time.monotonic() + self._command_timeout.total_seconds()
        while True:
            record = fetch()
            if record is not None:
                return record
            if time.monotonic() >= deadline:
                return None
            time.sleep(self._poll_interval.total_seconds())


def _outcome_from_record(record: CommandRecord, *, deduplicated: bool) -> SendOutcome:
    result: dict[str, Any] = record.result or {}
    client_message_id = record.client_message_id or ""

    if result.get("accepted"):
        outcome = SendOutcome.success(
            client_message_id=client_message_id,
            provider_message_id=result.get("provider_message_id") or record.wa_message_id or "",
            ack=DeliveryAck(result.get("ack", DeliveryAck.SERVER.value)),
        )
    else:
        outcome = SendOutcome.failure(
            client_message_id=client_message_id,
            code=result.get("error_code", "UNKNOWN"),
            detail=result.get("error_detail", ""),
            error_class=ErrorClass(result.get("error_class", ErrorClass.PERMANENT.value)),
        )
    return dataclasses.replace(outcome, deduplicated=deduplicated)


def _candidates_from_result(result: dict[str, Any] | None) -> Sequence[GroupCandidate]:
    if not result:
        return ()
    if result.get("error"):
        # The agent answered, but could not look groups up (most often: WhatsApp
        # dropped between claiming the request and answering it).
        raise ProviderUnavailableError(
            f"WhatsApp couldn't list your groups: {result.get('detail') or result['error']}.",
            op="resolve_group",
        )
    return tuple(
        GroupCandidate(
            external_jid=c["external_jid"],
            display_name=c["display_name"],
            member_count=c.get("member_count"),
            confidence=c.get("confidence", 0.0),
        )
        for c in result.get("candidates", [])
    )


def _send_payload(external_jid: str, body: str, image_png: bytes | None) -> dict[str, str]:
    """The agent sends an image with ``body`` as its caption when
    ``image_b64`` is present (apps/agent/src/commands.ts), plain text
    otherwise."""
    payload = {"external_jid": external_jid, "body": body}
    if image_png is not None:
        payload["image_b64"] = base64.b64encode(image_png).decode("ascii")
    return payload
