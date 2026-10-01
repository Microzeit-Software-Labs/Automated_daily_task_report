"""Review lifecycle and the /commit endpoint.

The three named actions (Update Only / Update + Share / Share Only) collapse
into this one endpoint with a ``mode`` discriminator -- see
``SharingService``'s module docstring for why keeping them as three separate
calls would itself be a source of inconsistency.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query, Response

from interlock.adapters.persistence.scheduler_repository import SchedulerRepository
from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.api import deps
from interlock.api.schemas import (
    ApprovalRequestOut,
    CommitRequest,
    CommitResponse,
    DeliverySummaryOut,
    PreviewResponse,
    ReviewDetailOut,
    ReviewListItemOut,
    ShareJobOut,
    ShareOut,
    ShareRecipientOut,
    SnoozeRequest,
)
from interlock.domain.common.actor import Actor
from interlock.domain.common.errors import NotFoundError
from interlock.services.review_service import ReviewService
from interlock.services.sharing_service import SharingService
from interlock.services.task_service import BulkUpdateItem

router = APIRouter(prefix="/approval-requests", tags=["approvals"])


@router.get("", response_model=list[ReviewListItemOut])
def list_reviews(
    local_date: dt.date | None = None,
    since: dt.date | None = Query(
        default=None, description="Only reviews for this day or later (the Reports period filter)."
    ),
    limit: int = Query(default=20, ge=1, le=100),
    service: ReviewService = Depends(deps.get_review_service),
    share_repo: ShareRepository = Depends(deps.get_share_repo),
) -> list[ReviewListItemOut]:
    """Newest first, each with how its latest send went. How a client finds
    today's 09:00 / 17:00 review, and the Reports list."""
    reviews = service.list_recent(limit=limit, day=local_date, since=since)
    deliveries = share_repo.delivery_summaries([r.id for r in reviews])
    items = []
    for review in reviews:
        summary = deliveries.get(review.id)
        items.append(
            ReviewListItemOut(
                **ApprovalRequestOut.from_entity(review).model_dump(),
                delivery=None if summary is None else DeliverySummaryOut.from_entity(summary),
            )
        )
    return items


@router.post("", response_model=ApprovalRequestOut, status_code=201)
def create_manual_review(
    actor: Actor = Depends(deps.get_current_actor),
    now: dt.datetime = Depends(deps.now_dep),
    service: ReviewService = Depends(deps.get_review_service),
) -> ApprovalRequestOut:
    """Start a MANUAL review now, outside the 09:00 / 17:00 schedule. Sends
    nothing by itself -- it still has to be approved through ``/commit``."""
    return ApprovalRequestOut.from_entity(service.create_manual(actor=actor, now=now))


@router.get("/{request_id}", response_model=ReviewDetailOut)
def get_review(
    request_id: str,
    service: ReviewService = Depends(deps.get_review_service),
    share_repo: ShareRepository = Depends(deps.get_share_repo),
    scheduler_repo: SchedulerRepository = Depends(deps.get_scheduler_repo),
) -> ReviewDetailOut:
    detail = service.get_detail(request_id)
    if detail is None:
        raise NotFoundError(
            f"Approval request {request_id} does not exist.", request_id=request_id
        )
    shares = []
    for job in share_repo.list_jobs_for_review(request_id):
        action = scheduler_repo.get(job.scheduled_action_id)
        snapshot = share_repo.get_snapshot(job.snapshot_id)
        shares.append(
            ShareOut(
                job=ShareJobOut.from_entity(job),
                run_at=action.run_at if action else None,
                rendered_body=snapshot.rendered_body if snapshot else "",
                snapshot_id=job.snapshot_id,
                has_image=bool(snapshot and snapshot.rendered_image),
                recipients=[
                    ShareRecipientOut.from_entity(r)
                    for r in share_repo.list_recipients_for_job(job.id)
                ],
            )
        )
    return ReviewDetailOut.from_detail(detail, shares)


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


@router.post("/{request_id}/snooze", response_model=ApprovalRequestOut)
def snooze_review(
    request_id: str,
    payload: SnoozeRequest,
    actor: Actor = Depends(deps.get_current_actor),
    now: dt.datetime = Depends(deps.now_dep),
    service: ReviewService = Depends(deps.get_review_service),
) -> ApprovalRequestOut:
    """Quiet the popup for this review, then ask again after ``minutes``."""
    request = service.snooze(request_id, minutes=payload.minutes, actor=actor, now=now)
    return ApprovalRequestOut.from_entity(request)


@router.post("/{request_id}/preview", response_model=PreviewResponse)
def preview_review(
    request_id: str,
    now: dt.datetime = Depends(deps.now_dep),
    service: SharingService = Depends(deps.get_sharing_service),
) -> PreviewResponse:
    """Render without approving -- a pure function of current data, safe to
    call repeatedly as the user toggles recipients before committing."""
    result = service.preview(request_id, now=now)
    return PreviewResponse(
        rendered_body=result.body, content_hash=result.content_hash, has_image=result.has_image
    )


@router.get(
    "/{request_id}/preview.png",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
def preview_image(
    request_id: str,
    now: dt.datetime = Depends(deps.now_dep),
    service: SharingService = Depends(deps.get_sharing_service),
) -> Response:
    """The table image a commit right now would freeze. 404 in text format."""
    image = service.preview_image(request_id, now=now)
    if image is None:
        raise NotFoundError("Reports are sent as text; there is no image.", request_id=request_id)
    return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})


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
        send_at=(
            now + dt.timedelta(minutes=payload.delay_minutes)
            if payload.delay_minutes is not None
            else payload.send_at
        ),
        template_id=payload.template_id,
        template_source=payload.template_source,
        expected_content_hash=payload.expected_content_hash,
    )
    return CommitResponse.from_result(result)
