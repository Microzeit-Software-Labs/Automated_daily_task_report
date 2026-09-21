"""State transitions.

There is exactly one way to change the state of an approval, a share job or a
recipient: call :func:`transition`. Nothing else in the codebase assigns to a
``state`` attribute, and a CI check enforces that.

Two invariants are enforced here rather than in a service, so that no call path
can bypass them:

1. **Only legal transitions happen.** The tables below are the complete truth.
2. **Only a human can approve.** Moving into ``APPROVED`` with a system actor
   raises :class:`ApprovalRequired`. This is the product's central rule, and it
   is enforced at the narrowest possible point.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, Final

from interlock.domain.approvals.states import (
    HUMAN_ONLY_TARGETS,
    TERMINAL_APPROVAL_STATES,
    TERMINAL_JOB_STATES,
    TERMINAL_RECIPIENT_STATES,
    ApprovalState,
    RecipientState,
    ShareJobState,
)
from interlock.domain.common.actor import Actor
from interlock.domain.common.clock import ensure_aware
from interlock.domain.common.errors import (
    ApprovalRequiredError,
    IllegalTransitionError,
)

type AnyState = ApprovalState | ShareJobState | RecipientState


def _table[S: AnyState](mapping: dict[S, set[S]]) -> Mapping[S, frozenset[S]]:
    return MappingProxyType({key: frozenset(value) for key, value in mapping.items()})


# ---------------------------------------------------------------------------
# A. ApprovalRequest
# ---------------------------------------------------------------------------
APPROVAL_TRANSITIONS: Final[Mapping[ApprovalState, frozenset[ApprovalState]]] = _table(
    {
        ApprovalState.DRAFT: {ApprovalState.REVIEW_PENDING, ApprovalState.CANCELLED},
        ApprovalState.REVIEW_PENDING: {
            ApprovalState.USER_EDITING,
            ApprovalState.READY,
            # Straight to APPROVED too: /commit is one atomic "click Update +
            # Share / Share Only" action -- apply edits, freeze, approve,
            # schedule -- with no separate "mark this ready" round trip in
            # front of it. READY exists for a UI that wants an explicit
            # preview step in between; it is a waypoint /commit may pass
            # through, never one it is forced to.
            ApprovalState.APPROVED,
            ApprovalState.CLOSED_NO_SHARE,
            ApprovalState.CANCELLED,
            ApprovalState.EXPIRED,
        },
        ApprovalState.USER_EDITING: {
            ApprovalState.READY,
            ApprovalState.APPROVED,  # see the note on REVIEW_PENDING above
            ApprovalState.CLOSED_NO_SHARE,
            ApprovalState.CANCELLED,
            ApprovalState.EXPIRED,
        },
        ApprovalState.READY: {
            # Back to editing if the user changes their mind at the preview.
            ApprovalState.USER_EDITING,
            ApprovalState.APPROVED,
            ApprovalState.CLOSED_NO_SHARE,
            ApprovalState.CANCELLED,
            ApprovalState.EXPIRED,
        },
        ApprovalState.APPROVED: set(),
        ApprovalState.CLOSED_NO_SHARE: set(),
        ApprovalState.CANCELLED: set(),
        ApprovalState.EXPIRED: set(),
    }
)

# ---------------------------------------------------------------------------
# B. ShareJob
# ---------------------------------------------------------------------------
SHARE_JOB_TRANSITIONS: Final[Mapping[ShareJobState, frozenset[ShareJobState]]] = _table(
    {
        ShareJobState.PENDING: {
            ShareJobState.SCHEDULED,
            ShareJobState.SENDING,  # "Send now"
            ShareJobState.CANCELLED,
        },
        ShareJobState.SCHEDULED: {
            ShareJobState.SENDING,
            ShareJobState.RESCHEDULED,
            ShareJobState.DEFERRED,
            ShareJobState.SUPERSEDED,
            ShareJobState.CANCELLED,
        },
        ShareJobState.RESCHEDULED: {ShareJobState.SCHEDULED, ShareJobState.CANCELLED},
        ShareJobState.DEFERRED: {
            ShareJobState.SCHEDULED,
            ShareJobState.SENDING,
            ShareJobState.CANCELLED,
        },
        ShareJobState.SENDING: {
            ShareJobState.SENT,
            ShareJobState.PARTIALLY_SENT,
            ShareJobState.FAILED,
        },
        # SENT is terminal on purpose. Nothing failed, so there is nothing to
        # retry, and allowing SENT -> SENDING would make a duplicate send
        # reachable through a UI misclick.
        ShareJobState.SENT: set(),
        ShareJobState.PARTIALLY_SENT: {ShareJobState.SENDING},
        ShareJobState.FAILED: {ShareJobState.SENDING},
        ShareJobState.CANCELLED: set(),
        ShareJobState.SUPERSEDED: set(),
    }
)

# ---------------------------------------------------------------------------
# C. ShareRecipient
# ---------------------------------------------------------------------------
RECIPIENT_TRANSITIONS: Final[Mapping[RecipientState, frozenset[RecipientState]]] = _table(
    {
        RecipientState.QUEUED: {
            RecipientState.SENDING,
            RecipientState.SKIPPED,
            RecipientState.CANCELLED,
        },
        RecipientState.SENDING: {RecipientState.SENT, RecipientState.FAILED},
        RecipientState.FAILED: {RecipientState.QUEUED},
        RecipientState.SENT: set(),
        RecipientState.SKIPPED: set(),
        RecipientState.CANCELLED: set(),
    }
)

# Registries keyed by state type. Deliberately typed with `Any` for the state:
# Mapping and frozenset are both invariant, so `object` cannot describe a
# heterogeneous registry. The public functions below re-narrow via their generic
# signatures, so callers still get precise types.
_ALL_TABLES: Final[dict[type, Mapping[Any, frozenset[Any]]]] = {
    ApprovalState: APPROVAL_TRANSITIONS,
    ShareJobState: SHARE_JOB_TRANSITIONS,
    RecipientState: RECIPIENT_TRANSITIONS,
}

_TERMINALS: Final[dict[type, frozenset[Any]]] = {
    ApprovalState: TERMINAL_APPROVAL_STATES,
    ShareJobState: TERMINAL_JOB_STATES,
    RecipientState: TERMINAL_RECIPIENT_STATES,
}


@dataclasses.dataclass(frozen=True, slots=True)
class TransitionRecord:
    """The audit payload for one state change."""

    entity_type: str
    entity_id: str
    from_state: str
    to_state: str
    actor: str
    actor_kind: str
    at: dt.datetime
    reason: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "actor": self.actor,
            "actor_kind": self.actor_kind,
            "at": self.at.isoformat(),
            "reason": self.reason,
        }


def legal_targets[S: AnyState](current: S) -> frozenset[S]:
    """Every state reachable from ``current`` in one step."""
    table = _ALL_TABLES[type(current)]
    result: frozenset[S] = table[current]
    return result


def is_terminal[S: AnyState](state: S) -> bool:
    return state in _TERMINALS[type(state)]


def can_transition[S: AnyState](current: S, target: S) -> bool:
    return target in legal_targets(current)


def assert_transition[S: AnyState](
    current: S,
    target: S,
    *,
    entity_type: str,
    entity_id: str,
    actor: Actor,
) -> None:
    """Raise unless ``current -> target`` is legal for ``actor``.

    Separated from :func:`transition` so a caller can validate before starting a
    database transaction.
    """
    if not can_transition(current, target):
        raise IllegalTransitionError(
            f"{entity_type} {entity_id} cannot move from {current.value} to {target.value}.",
            entity_type=entity_type,
            entity_id=entity_id,
            from_state=current.value,
            to_state=target.value,
            legal_targets=sorted(state.value for state in legal_targets(current)),
        )

    if target in HUMAN_ONLY_TARGETS and not actor.is_human:
        raise ApprovalRequiredError(
            f"{target.value} requires a human approver; "
            f"{actor} is a {actor.kind.value.lower()} actor.",
            entity_type=entity_type,
            entity_id=entity_id,
            to_state=target.value,
            actor_kind=actor.kind.value,
        )


def transition[S: AnyState](
    current: S,
    target: S,
    *,
    entity_type: str,
    entity_id: str,
    actor: Actor,
    at: dt.datetime,
    reason: str | None = None,
) -> TransitionRecord:
    """Validate and record a state change.

    Returns the audit record; the caller is responsible for persisting both the
    new state and the record in the same transaction.
    """
    ensure_aware(at, field="transition.at")
    assert_transition(
        current, target, entity_type=entity_type, entity_id=entity_id, actor=actor
    )
    return TransitionRecord(
        entity_type=entity_type,
        entity_id=entity_id,
        from_state=current.value,
        to_state=target.value,
        actor=str(actor),
        actor_kind=actor.kind.value,
        at=at,
        reason=reason,
    )


def roll_up_job_state(recipient_states: Iterable[RecipientState]) -> ShareJobState:
    """Derive a job's outcome from its recipients.

    Never asserted directly -- a job is SENT because every recipient succeeded,
    not because something decided to call it SENT. This is what keeps "2 sent,
    1 failed" honest.
    """
    states = list(recipient_states)
    if not states:
        return ShareJobState.FAILED

    if any(state is RecipientState.SENDING for state in states):
        return ShareJobState.SENDING
    if any(state is RecipientState.QUEUED for state in states):
        return ShareJobState.SENDING

    delivered = sum(1 for state in states if state is RecipientState.SENT)
    # A skipped group is not a failure -- it was deliberately excluded.
    considered = sum(1 for state in states if state is not RecipientState.SKIPPED)

    if considered == 0:
        return ShareJobState.CANCELLED
    if delivered == considered:
        return ShareJobState.SENT
    if delivered == 0:
        return ShareJobState.FAILED
    return ShareJobState.PARTIALLY_SENT
