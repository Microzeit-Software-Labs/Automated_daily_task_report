"""The three lifecycles.

The product brief described a single twelve-state machine. Implemented that way
it would be wrong: ``PARTIALLY_SENT`` describes a *delivery* while
``USER_EDITING`` describes an *approval*, and one review can produce several
sends, each fanning out to several recipients with independent outcomes.
Collapsing three lifetimes into one enum makes "two sent, one failed, retry only
the failed one" unrepresentable.

So: three machines, each owning exactly one lifetime.
"""

from __future__ import annotations

from enum import StrEnum


class ApprovalState(StrEnum):
    """One scheduled review. Ends when the user decides what to do with it."""

    DRAFT = "DRAFT"
    REVIEW_PENDING = "REVIEW_PENDING"
    USER_EDITING = "USER_EDITING"
    READY = "READY"

    APPROVED = "APPROVED"
    """The gate. Entering this state requires a human actor, a frozen snapshot
    and at least one recipient. Terminal for the approval itself -- delivery
    continues in a ShareJob."""

    CLOSED_NO_SHARE = "CLOSED_NO_SHARE"
    """"Update Only" finished successfully. A first-class outcome, not an
    abandonment -- the user reviewed their tasks and chose not to share."""

    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    """Untouched by the time the next review was created."""


class ShareJobState(StrEnum):
    """One approved send. Fans out to one or more recipients."""

    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    RESCHEDULED = "RESCHEDULED"

    DEFERRED = "DEFERRED"
    """Came due, but the frozen report was no longer valid to send unattended --
    the laptop was asleep past the grace window, or the calendar day rolled over.
    Requires a fresh human decision; never auto-sends."""

    SENDING = "SENDING"
    SENT = "SENT"
    PARTIALLY_SENT = "PARTIALLY_SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    SUPERSEDED = "SUPERSEDED"
    """A newer approved report for the same review replaced this one before it
    fired."""


class RecipientState(StrEnum):
    """One group, one job. The actual unit of delivery."""

    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    """The group was disabled between approval and send."""
    CANCELLED = "CANCELLED"


# States only a human may move an entity into. Enforced by the state machine.
HUMAN_ONLY_TARGETS: frozenset[ApprovalState] = frozenset({ApprovalState.APPROVED})

TERMINAL_APPROVAL_STATES: frozenset[ApprovalState] = frozenset(
    {
        ApprovalState.APPROVED,
        ApprovalState.CLOSED_NO_SHARE,
        ApprovalState.CANCELLED,
        ApprovalState.EXPIRED,
    }
)

TERMINAL_JOB_STATES: frozenset[ShareJobState] = frozenset(
    {ShareJobState.SENT, ShareJobState.CANCELLED, ShareJobState.SUPERSEDED}
)

TERMINAL_RECIPIENT_STATES: frozenset[RecipientState] = frozenset(
    {RecipientState.SENT, RecipientState.SKIPPED, RecipientState.CANCELLED}
)

RETRYABLE_JOB_STATES: frozenset[ShareJobState] = frozenset(
    {ShareJobState.PARTIALLY_SENT, ShareJobState.FAILED}
)
