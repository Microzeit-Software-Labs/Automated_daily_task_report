"""In-memory WhatsApp provider.

Every acceptance scenario runs against this. It exists so that duplicate sends,
partial failures, retries and ambiguous timeouts can be tested exhaustively and
deterministically, offline, in milliseconds -- none of which is possible against
a real WhatsApp session.

It is also the reference implementation of the provider contract. In particular
it enforces the idempotency requirement: a repeated ``client_message_id`` returns
the original outcome and does *not* send again. A real provider that fails to do
this will pass its own tests and then send your team two copies of every report.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence

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


@dataclasses.dataclass(frozen=True, slots=True)
class SentMessage:
    """One delivery the mock actually performed."""

    external_jid: str
    body: str
    client_message_id: str
    at: dt.datetime
    image_png: bytes | None = None


@dataclasses.dataclass(slots=True)
class _FailureRule:
    code: str
    detail: str
    error_class: ErrorClass
    remaining: int | None
    """``None`` means fail forever."""


class MockWhatsAppProvider:
    """A WhatsAppProvider that records instead of sending.

    Test controls are prefixed to keep them visually distinct from the four
    methods that make up the real provider contract.
    """

    name = "mock"

    def __init__(
        self,
        clock: Clock,
        *,
        state: ConnectionState = ConnectionState.CONNECTED,
    ) -> None:
        self._clock = clock
        self._state = state
        self._groups: dict[str, GroupCandidate] = {}
        self._failures: dict[str, _FailureRule] = {}
        self._outcomes: dict[str, SendOutcome] = {}
        self._sent: list[SentMessage] = []
        self._last_success: dt.datetime | None = None
        self._send_attempts = 0
        self._reason: str | None = None
        self._account_jid: str | None = None
        self._account_name: str | None = None
        self._link = LinkStatus(state=PairingState.IDLE)
        self._qr_counter = 0
        self._agent_online = True

    # -- test controls ------------------------------------------------------

    def given_link_problem(self, reason: str) -> None:
        """Make the link look unusable for ``reason`` (NOT_LINKED, LOGGED_OUT,
        SESSION_INVALID, REPLACED, FORBIDDEN), as the real agent reports it."""
        self._state = (
            ConnectionState.UNAVAILABLE
            if reason in {"REPLACED", "FORBIDDEN"}
            else ConnectionState.LOGIN_REQUIRED
        )
        self._reason = reason

    def given_agent_offline(self) -> None:
        """The WhatsApp service itself isn't running: nothing can be linked."""
        self._agent_online = False
        self._state = ConnectionState.UNAVAILABLE
        self._reason = "AGENT_OFFLINE"

    def complete_link(
        self, jid: str = "919999999999:1@s.whatsapp.net", name: str = "Test phone"
    ) -> None:
        """Play the phone scanning the QR: the attempt succeeds and the new
        account becomes the connected one."""
        self._link = dataclasses.replace(
            self._link, state=PairingState.SUCCEEDED, qr=None, detail=f"Linked {jid}."
        )
        self._state = ConnectionState.CONNECTED
        self._reason = None
        self._account_jid, self._account_name = jid, name

    def expire_link(self) -> None:
        self._link = dataclasses.replace(
            self._link, state=PairingState.EXPIRED, qr=None, detail="The QR code expired."
        )

    def advance_qr(self) -> None:
        """WhatsApp rotates the QR every ~20 s."""
        self._qr_counter += 1
        self._link = dataclasses.replace(
            self._link, qr=f"mock-qr-{self._qr_counter}", qr_at=self._clock.now()
        )

    def given_group(
        self, display_name: str, external_jid: str, *, members: int | None = None
    ) -> GroupCandidate:
        candidate = GroupCandidate(
            external_jid=external_jid,
            display_name=display_name,
            member_count=members,
            confidence=1.0,
        )
        self._groups[external_jid] = candidate
        return candidate

    def given_connection(self, state: ConnectionState) -> None:
        self._state = state

    def given_failure(
        self,
        external_jid: str,
        *,
        code: str = "GROUP_NOT_FOUND",
        detail: str = "group could not be resolved",
        error_class: ErrorClass = ErrorClass.PERMANENT,
        times: int | None = None,
    ) -> None:
        """Make sends to ``external_jid`` fail.

        ``times=None`` fails forever; ``times=2`` fails twice then succeeds,
        which is how the retry ladder is exercised.
        """
        self._failures[external_jid] = _FailureRule(
            code=code, detail=detail, error_class=error_class, remaining=times
        )

    def clear_failures(self) -> None:
        self._failures.clear()

    @property
    def sent(self) -> Sequence[SentMessage]:
        """Every message actually transmitted, in order."""
        return tuple(self._sent)

    @property
    def send_attempts(self) -> int:
        """Calls to send_text, including deduplicated replays and failures.

        The gap between this and ``len(sent)`` is what proves idempotency:
        two attempts, one message.
        """
        return self._send_attempts

    def sent_to(self, external_jid: str) -> Sequence[SentMessage]:
        return tuple(m for m in self._sent if m.external_jid == external_jid)

    def delivered_count(self, external_jid: str) -> int:
        return len(self.sent_to(external_jid))

    # -- provider contract --------------------------------------------------

    def health(self) -> ProviderStatus:
        return ProviderStatus(
            state=self._state,
            checked_at=self._clock.now(),
            provider=self.name,
            last_successful_send_at=self._last_success,
            detail="in-memory test provider",
            reason=self._reason,
            account_jid=self._account_jid,
            account_name=self._account_name,
        )

    # -- linking (WhatsAppLinking) ----------------------------------------------

    def start_link(self) -> LinkStatus:
        self._require_agent_online()
        self._qr_counter += 1
        self._link = LinkStatus(
            state=PairingState.WAITING_FOR_SCAN,
            detail="Scan the code with WhatsApp.",
            pairing_id=new_id(),
            qr=f"mock-qr-{self._qr_counter}",
            qr_at=self._clock.now(),
        )
        return self._link

    def cancel_link(self) -> LinkStatus:
        if self._link.state.in_progress:
            self._link = dataclasses.replace(
                self._link, state=PairingState.CANCELLED, qr=None, detail="Linking was cancelled."
            )
        return self._link

    def link_status(self) -> LinkStatus:
        return dataclasses.replace(self._link, agent_online=self._agent_online)

    def _require_agent_online(self) -> None:
        if not self._agent_online:
            raise ProviderUnavailableError(
                "Interlock's WhatsApp service isn't running, so there is nothing to "
                "link yet. Start Interlock and try again."
            )

    def reconnect(self) -> None:
        self._require_agent_online()
        if self._reason == "NOT_LINKED":
            raise ProviderUnavailableError(
                "WhatsApp isn't linked to a phone yet. Link one with a QR code instead."
            )
        self._state = ConnectionState.CONNECTED
        self._reason = None

    def resolve_group(self, name: str) -> Sequence[GroupCandidate]:
        """Match on name, returning every plausible candidate.

        Deliberately returns all near-matches rather than the best one, so the
        ambiguity path ("SI Team" vs "SI Team - Bangalore") is exercised by
        default rather than being an afterthought.
        """
        needle = name.strip().casefold()
        if not needle:
            return ()

        matches: list[GroupCandidate] = []
        for group in self._groups.values():
            haystack = group.display_name.casefold()
            if haystack == needle:
                confidence = 1.0
            elif haystack.startswith(needle):
                confidence = 0.8
            elif needle in haystack:
                confidence = 0.6
            else:
                continue
            matches.append(dataclasses.replace(group, confidence=confidence))

        return tuple(sorted(matches, key=lambda c: (-c.confidence, c.display_name)))

    def send_text(
        self,
        *,
        external_jid: str,
        body: str,
        client_message_id: str,
        image_png: bytes | None = None,
    ) -> SendOutcome:
        self._send_attempts += 1

        # --- Idempotency, before anything else.
        # A replay must not re-check connection state or consume a failure rule;
        # it returns exactly what the first attempt returned.
        if client_message_id in self._outcomes:
            previous = self._outcomes[client_message_id]
            return dataclasses.replace(previous, deduplicated=True)

        if not self._state.can_send:
            # Not recorded: the send never happened, so a later retry with the
            # same id should genuinely attempt it.
            return SendOutcome.failure(
                client_message_id=client_message_id,
                code="PROVIDER_UNAVAILABLE",
                detail=f"WhatsApp is {self._state.value}",
                error_class=ErrorClass.TRANSIENT,
            )

        rule = self._failures.get(external_jid)
        if rule is not None:
            if rule.remaining is None:
                return self._fail(client_message_id, rule)
            if rule.remaining > 0:
                rule.remaining -= 1
                return self._fail(client_message_id, rule)

        now = self._clock.now()
        self._sent.append(
            SentMessage(
                external_jid=external_jid,
                body=body,
                client_message_id=client_message_id,
                at=now,
                image_png=image_png,
            )
        )
        self._last_success = now

        outcome = SendOutcome.success(
            client_message_id=client_message_id,
            provider_message_id=f"mock-{len(self._sent):06d}",
            ack=DeliveryAck.SERVER,
        )
        self._outcomes[client_message_id] = outcome
        return outcome

    def delivery_state(self, client_message_id: str) -> SendOutcome | None:
        """Resolve an ambiguous timeout without sending again."""
        return self._outcomes.get(client_message_id)

    # -- internals ----------------------------------------------------------

    def _fail(self, client_message_id: str, rule: _FailureRule) -> SendOutcome:
        outcome = SendOutcome.failure(
            client_message_id=client_message_id,
            code=rule.code,
            detail=rule.detail,
            error_class=rule.error_class,
        )
        # Permanent failures are remembered so a retry returns the same verdict
        # rather than silently succeeding later. Transient ones are not, because
        # retrying them is the entire point.
        if rule.error_class is ErrorClass.PERMANENT:
            self._outcomes[client_message_id] = outcome
        return outcome
