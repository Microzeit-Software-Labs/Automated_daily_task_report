"""Who did a thing.

Every state change records an actor. The distinction between a human and the
system is load-bearing rather than cosmetic: the state machine refuses to move a
report into APPROVED unless the actor is a real person. That is the mechanical
expression of this product's central rule -- the scheduler can prepare a report,
but only a human can authorise sending it.
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum


class ActorKind(StrEnum):
    USER = "USER"
    """A signed-in person taking a deliberate action."""

    SYSTEM = "SYSTEM"
    """The scheduler, a worker, a migration. Never an approver."""

    SYNC = "SYNC"
    """An inbound change from Google Sheets or Excel."""

    AGENT = "AGENT"
    """The local WhatsApp agent reporting a delivery outcome."""


@dataclasses.dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    id: str
    label: str = ""

    @property
    def is_human(self) -> bool:
        return self.kind is ActorKind.USER

    def __str__(self) -> str:
        return self.label or f"{self.kind.value.lower()}:{self.id}"


def system_actor(component: str) -> Actor:
    return Actor(kind=ActorKind.SYSTEM, id=component, label=f"system:{component}")


def sync_actor(source: str) -> Actor:
    return Actor(kind=ActorKind.SYNC, id=source, label=f"sync:{source}")


def agent_actor(device_id: str) -> Actor:
    return Actor(kind=ActorKind.AGENT, id=device_id, label=f"agent:{device_id}")
