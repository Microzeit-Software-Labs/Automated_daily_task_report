"""Retrying a failed delivery.

Scenario 8: SI Team and Management succeed, AI Engineering fails. Retrying
must touch only the named recipient -- the two successes are untouched
regardless, since ``SENT`` is a terminal state in the recipient machine (see
``domain/approvals/machine.py``) with no legal transition back out of it.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Response

from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.api import deps
from interlock.api.schemas import ShareRecipientOut
from interlock.domain.common.errors import NotFoundError
from interlock.services.scheduler_service import SchedulerService

router = APIRouter(prefix="/shares", tags=["shares"])


@router.post("/recipients/{recipient_id}/retry", response_model=ShareRecipientOut)
def retry_recipient(
    recipient_id: str,
    now: dt.datetime = Depends(deps.now_dep),
    service: SchedulerService = Depends(deps.get_scheduler_service),
) -> ShareRecipientOut:
    recipient = service.retry_recipient(recipient_id, now=now)
    return ShareRecipientOut.from_entity(recipient)


@router.get(
    "/snapshots/{snapshot_id}/image.png",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
def snapshot_image(
    snapshot_id: str, repo: ShareRepository = Depends(deps.get_share_repo)
) -> Response:
    """The exact image that was approved and sent. Immutable, so cacheable."""
    snapshot = repo.get_snapshot(snapshot_id)
    if snapshot is None or snapshot.rendered_image is None:
        raise NotFoundError("No image for this snapshot.", snapshot_id=snapshot_id)
    return Response(
        content=snapshot.rendered_image,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
