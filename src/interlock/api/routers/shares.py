"""Retrying a failed delivery.

Scenario 8: SI Team and Management succeed, AI Engineering fails. Retrying
must touch only the named recipient -- the two successes are untouched
regardless, since ``SENT`` is a terminal state in the recipient machine (see
``domain/approvals/machine.py``) with no legal transition back out of it.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends

from interlock.api import deps
from interlock.api.schemas import ShareRecipientOut
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
