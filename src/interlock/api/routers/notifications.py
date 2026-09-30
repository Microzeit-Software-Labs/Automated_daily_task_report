"""What the popup asks the server: "is there a report waiting on me?"

Stateless on the client. The answer is derived from the open review in
Postgres (``domain/approvals/prompt.py``), so a restart, a second browser tab,
or a page refresh can neither lose the prompt nor show it twice.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends

from interlock.api import deps
from interlock.api.schemas import PromptOut
from interlock.services.review_service import ReviewService

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/prompt", response_model=PromptOut)
def current_prompt(
    now: dt.datetime = Depends(deps.now_dep),
    service: ReviewService = Depends(deps.get_review_service),
) -> PromptOut:
    return PromptOut.from_request(service.active_prompt(now=now))
