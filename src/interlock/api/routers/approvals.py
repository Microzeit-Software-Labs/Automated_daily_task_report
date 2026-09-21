"""Review lifecycle and the /commit endpoint.

The three named actions (Update Only / Update + Share / Share Only) collapse
into this one endpoint with a ``mode`` discriminator -- see
``SharingService``'s module docstring for why keeping them as three separate
calls would itself be a source of inconsistency.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends

from interlock.api import deps
from interlock.api.schemas import (
    ApprovalRequestOut,
    CommitRequest,
    CommitResponse,
    PreviewResponse,
    ReviewDetailOut,
)
from interlock.domain.common.actor import Actor
from interlock.domain.common.errors import NotFoundError
from interlock.services.review_service import ReviewService
from interlock.services.sharing_service import SharingService
from interlock.services.task_service import BulkUpdateItem

router = APIRouter(prefix="/approval-requests", tags=["approvals"])


@router.get("/{request_id}", response_model=ReviewDetailOut)
def get_review(
    request_id: str, service: ReviewService = Depends(deps.get_review_service)
) -> ReviewDetailOut:
    detail = service.get_detail(request_id)
    if detail is None:
        raise NotFoundError(
            f"Approval request {request_id} does not exist.", request_id=request_id
        )
    return ReviewDetailOut.from_detail(detail)


@router.post("/{request_id}/open", response_model=ApprovalRequestOut)
def open_review(
    request_id: str,
    actor: Actor = Depends(deps.get_current_actor),
    now: dt.datetime = Depends(deps.now_dep),
    service: ReviewService = Depends(deps.get_review_service),
) -> ApprovalRequestOut:
    request = service.open(request_id, actor=actor, now=now)
    return ApprovalRequestOut.from_entity(request)


@router.post("/{request_id}/cancel", response_model=ApprovalRequestOut)
def cancel_review(
    request_id: str,
    actor: Actor = Depends(deps.get_current_actor),
    now: dt.datetime = Depends(deps.now_dep),
    service: ReviewService = Depends(deps.get_review_service),
) -> ApprovalRequestOut:
    request = service.cancel(request_id, actor=actor, now=now)
    return ApprovalRequestOut.from_entity(request)


@router.post("/{request_id}/preview", response_model=PreviewResponse)
def preview_review(
    request_id: str,
    now: dt.datetime = Depends(deps.now_dep),
    service: SharingService = Depends(deps.get_sharing_service),
) -> PreviewResponse:
    """Render without approving -- a pure function of current data, safe to
    call repeatedly as the user toggles recipients before committing."""
    body = service.preview(request_id, now=now)
    return PreviewResponse(rendered_body=body)


@router.post("/{request_id}/commit", response_model=CommitResponse)
def commit_review(
    request_id: str,
    payload: CommitRequest,
    actor: Actor = Depends(deps.get_current_actor),
    now: dt.datetime = Depends(deps.now_dep),
    service: SharingService = Depends(deps.get_sharing_service),
) -> CommitResponse:
    task_updates = [
        BulkUpdateItem(
            task_id=item.task_id,
            changes=item.changes.model_dump(exclude_unset=True),
            expected_version=item.expected_version,
        )
        for item in payload.task_updates
    ]
    result = service.commit(
        approval_request_id=request_id,
        mode=payload.mode,
        action_version=payload.action_version,
        actor=actor,
        now=now,
        task_updates=task_updates,
        recipient_group_ids=payload.recipient_group_ids,
        send_at=payload.send_at,
        template_id=payload.template_id,
        template_source=payload.template_source,
    )
    return CommitResponse.from_result(result)
