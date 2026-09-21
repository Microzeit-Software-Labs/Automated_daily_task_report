"""Domain errors.

Each error carries a stable machine-readable ``code``. The frontend switches on
these codes; they are part of the API contract and must not be renamed casually.
The HTTP layer translates these into the error envelope -- the domain itself
never imports anything web-related.
"""

from __future__ import annotations

from typing import Any, ClassVar


class DomainError(Exception):
    """Base class for every expected failure in the domain."""

    code: ClassVar[str] = "DOMAIN_ERROR"
    http_status: ClassVar[int] = 400
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


class NotFoundError(DomainError):
    code = "NOT_FOUND"
    http_status = 404


class ValidationFailedError(DomainError):
    code = "VALIDATION_FAILED"
    http_status = 422


class IllegalTransitionError(DomainError):
    """An attempt to move an entity into a state it cannot legally reach.

    Raised by the state machine. Seeing this in logs means a caller tried to
    skip a step -- e.g. sending a job that was never approved.
    """

    code = "ILLEGAL_STATE_TRANSITION"
    http_status = 409


class TaskVersionConflictError(DomainError):
    """Optimistic concurrency failure: the task changed under the caller.

    The response carries both versions so the UI can show a merge prompt rather
    than a generic error.
    """

    code = "TASK_VERSION_CONFLICT"
    http_status = 412


class DatasetVersionConflictError(DomainError):
    """The task data moved between opening a review and committing it."""

    code = "DATASET_VERSION_CONFLICT"
    http_status = 409


class IdempotencyKeyReusedError(DomainError):
    """Same idempotency key, different request body.

    Deliberately an error rather than a silent replay: the caller believes it is
    repeating a request it is not actually repeating.
    """

    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 422


class ApprovalRequiredError(DomainError):
    """Something tried to reach the delivery path without a human approval.

    This is the guard on the product's central business rule. It should never
    fire in normal operation; if it does, treat it as a security event.
    """

    code = "APPROVAL_REQUIRED"
    http_status = 403


class NoRecipientsSelectedError(DomainError):
    code = "NO_RECIPIENTS_SELECTED"
    http_status = 422


class TemplateInvalidError(DomainError):
    code = "TEMPLATE_INVALID"
    http_status = 422


class MessageTooLongError(DomainError):
    code = "MESSAGE_TOO_LONG"
    http_status = 422


class ProviderUnavailableError(DomainError):
    """The delivery provider is not reachable right now. Retryable."""

    code = "PROVIDER_UNAVAILABLE"
    http_status = 503
    retryable = True


class GroupNotFoundError(DomainError):
    """A configured group no longer resolves. Permanent -- do not retry."""

    code = "GROUP_NOT_FOUND"
    http_status = 404


class SyncConflictAlreadyResolvedError(DomainError):
    """An attempt to resolve a sync conflict that was already resolved.

    Mirrors TaskVersionConflictError's shape: the caller's belief about what
    they were resolving is stale, so this is a 409, not a generic 404/422.
    """

    code = "SYNC_CONFLICT_ALREADY_RESOLVED"
    http_status = 409
