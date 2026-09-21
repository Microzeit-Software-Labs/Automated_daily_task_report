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

    @property
    def can_send(self) -> bool:
        return self.state.can_send


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
    ) -> SendOutcome:
        """Send ``body`` to one group. Must be idempotent on ``client_message_id``."""
        ...

    def delivery_state(self, client_message_id: str) -> SendOutcome | None:
        """Look up a previous send. Resolves the AMBIGUOUS case without resending."""
        ...
