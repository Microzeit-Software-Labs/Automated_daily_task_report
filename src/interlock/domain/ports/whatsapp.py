"""The WhatsApp delivery port.

Nothing above this interface knows whether messages travel via Baileys, the
official Cloud API, or a mock. That matters more than usual here: the delivery
mechanism for group messages is unofficial and may have to be replaced at short
notice, so the seam has to be real rather than aspirational.

Two design decisions worth stating:

* :meth:`resolve_group` returns a *list of candidates*. It never picks one. An
  internal report landing in the wrong group is unrecoverable, so a human
  confirms every match and the resolved JID is then pinned permanently.
* :meth:`send_text` takes a ``client_message_id``. The provider is required to
  treat a repeat of the same id as the same message and not send twice. This is
  the last line of duplicate defence, after the database compare-and-swap.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ConnectionState(StrEnum):
    CONNECTED = "CONNECTED"
    CONNECTING = "CONNECTING"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"
    AUTOMATION_ERROR = "AUTOMATION_ERROR"

    @property
    def can_send(self) -> bool:
        return self is ConnectionState.CONNECTED


class DeliveryAck(StrEnum):
    """How far a message got.

    Mirrors the acknowledgement levels the WhatsApp protocol actually exposes.
    ``SERVER`` is the first level that means "it left this machine" -- anything
    below that has not been delivered.
    """

    NONE = "NONE"
    ERROR = "ERROR"
    PENDING = "PENDING"
    SERVER = "SERVER"
    DEVICE = "DEVICE"
    READ = "READ"

    @property
    def is_delivered(self) -> bool:
        return self in {DeliveryAck.SERVER, DeliveryAck.DEVICE, DeliveryAck.READ}


class ErrorClass(StrEnum):
    """Drives the retry ladder. Classified on arrival, never guessed later."""

    TRANSIENT = "TRANSIENT"
    """Network blip, throttling, agent offline. Retry with backoff."""

    PERMANENT = "PERMANENT"
    """Group gone, JID invalid, account banned. Retrying makes it worse."""

    AMBIGUOUS = "AMBIGUOUS"
    """Timed out after the send was issued. Do NOT blind-retry -- query
    delivery state by client_message_id first. This is exactly how duplicate
    messages happen in systems that get it wrong."""


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderStatus:
    state: ConnectionState
    checked_at: dt.datetime
    provider: str
    last_successful_send_at: dt.datetime | None = None
    detail: str = ""
    reason: str | None = None
    """Machine-readable why the link is not usable, for the UI to switch on:
    NOT_LINKED, LOGGED_OUT, SESSION_INVALID, REPLACED, FORBIDDEN, AGENT_OFFLINE.
    ``None`` while healthy or merely reconnecting."""
    account_jid: str | None = None
    """Who is linked (``919876543210:12@s.whatsapp.net``); kept after a
    disconnect so the UI can say "last connected as ..."."""
    account_name: str | None = None

    @property
    def can_send(self) -> bool:
        return self.state.can_send


def phone_from_jid(jid: str | None) -> str | None:
    """``919876543210:12@s.whatsapp.net`` -> ``+919876543210``; ``None`` if the
    id is not a user id."""
    match = re.match(r"^(\d+)(?::\d+)?@", jid or "")
    return f"+{match.group(1)}" if match else None


@dataclasses.dataclass(frozen=True, slots=True)
class GroupCandidate:
    """One possible match for a group name the user typed."""

    external_jid: str
    display_name: str
    member_count: int | None = None
    confidence: float = 0.0
    """0.0-1.0. Presentational only -- it orders the list, it never auto-picks."""


@dataclasses.dataclass(frozen=True, slots=True)
class SendOutcome:
    accepted: bool
    client_message_id: str
    ack: DeliveryAck = DeliveryAck.NONE
    provider_message_id: str | None = None
    error_code: str | None = None
    error_detail: str = ""
    error_class: ErrorClass | None = None
    deduplicated: bool = False
    """True when the provider recognised a repeat and did not send again."""

    @property
    def retryable(self) -> bool:
        return self.error_class is ErrorClass.TRANSIENT

    @classmethod
    def success(
        cls,
        *,
        client_message_id: str,
        provider_message_id: str,
        ack: DeliveryAck = DeliveryAck.SERVER,
        deduplicated: bool = False,
    ) -> SendOutcome:
        return cls(
            accepted=True,
            client_message_id=client_message_id,
            ack=ack,
            provider_message_id=provider_message_id,
            deduplicated=deduplicated,
        )

    @classmethod
    def failure(
        cls,
        *,
        client_message_id: str,
        code: str,
        detail: str,
        error_class: ErrorClass,
    ) -> SendOutcome:
        return cls(
            accepted=False,
            client_message_id=client_message_id,
            ack=DeliveryAck.ERROR,
            error_code=code,
            error_detail=detail,
            error_class=error_class,
        )


@runtime_checkable
class WhatsAppProvider(Protocol):
    name: str

    def health(self) -> ProviderStatus:
        """Current connection state. Cheap; called on a timer, not only at send."""
        ...

    def resolve_group(self, name: str) -> Sequence[GroupCandidate]:
        """Find groups matching ``name``. Returns every candidate, picks none."""
        ...

    def send_text(
        self,
        *,
        external_jid: str,
        body: str,
        client_message_id: str,
        image_png: bytes | None = None,
    ) -> SendOutcome:
        """Send ``body`` to one group. Must be idempotent on ``client_message_id``.

        With ``image_png``, the message is that picture and ``body`` is its
        caption."""
        ...

    def delivery_state(self, client_message_id: str) -> SendOutcome | None:
        """Look up a previous send. Resolves the AMBIGUOUS case without resending."""
        ...


class PairingState(StrEnum):
    """One attempt to link a phone, as the UI sees it."""

    IDLE = "IDLE"
    """No attempt running."""
    STARTING = "STARTING"
    WAITING_FOR_SCAN = "WAITING_FOR_SCAN"
    """A QR code is on offer; the phone has not scanned it yet."""
    SCANNED = "SCANNED"
    """The phone scanned it; the link is being finished."""
    SUCCEEDED = "SUCCEEDED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def in_progress(self) -> bool:
        return self in {
            PairingState.STARTING,
            PairingState.WAITING_FOR_SCAN,
            PairingState.SCANNED,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class LinkStatus:
    state: PairingState
    detail: str = ""
    pairing_id: str | None = None
    qr: str | None = None
    """The current QR payload (changes every ~20 s). Secret-ish: it is only
    ever turned into an image for the local UI, never returned as text."""
    qr_at: dt.datetime | None = None
    agent_online: bool = True
    """False when the WhatsApp service isn't running, so no QR can appear."""


@runtime_checkable
class WhatsAppLinking(Protocol):
    """Linking (or re-linking) a phone from the UI.

    Deliberately separate from :class:`WhatsAppProvider`: sending messages and
    managing the link are different jobs, and a provider that cannot be linked
    from the UI (the official Cloud API, say) simply does not implement this.
    """

    def start_link(self) -> LinkStatus:
        """Begin a QR pairing (replacing any attempt already running)."""
        ...

    def cancel_link(self) -> LinkStatus:
        """Abandon the attempt. Changes nothing about the current link."""
        ...

    def link_status(self) -> LinkStatus:
        """Progress of the attempt, including the current QR."""
        ...

    def reconnect(self) -> None:
        """Reconnect a link that is still valid (dropped or taken over)."""
        ...

