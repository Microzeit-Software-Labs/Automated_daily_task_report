"""Which Google Sheet the tasks are read from, changed the way a WhatsApp phone is:
look first (``POST /sheet/check`` reads the pasted link and saves nothing), then
switch (``PUT /sheet``, which re-checks and only then saves). A failed check
changes nothing; the sheet already in use keeps working until the new one has
been read successfully by the worker.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends

from interlock.api import deps
from interlock.api.schemas import SheetCheckOut, SheetCheckRequest, SheetSourceOut
from interlock.domain.common.actor import Actor
from interlock.services.sheet_source_service import SheetSourceService

router = APIRouter(prefix="/sheet", tags=["sheet"])


@router.get("", response_model=SheetSourceOut)
def get_sheet(
    now: dt.datetime = Depends(deps.now_dep),
    service: SheetSourceService = Depends(deps.get_sheet_source_service),
) -> SheetSourceOut:
    """The saved sheet and how the last read of it went."""
    return SheetSourceOut.from_state(service.current(now=now), changeable=service.changeable)


@router.post("/check", response_model=SheetCheckOut)
def check_sheet(
    payload: SheetCheckRequest,
    now: dt.datetime = Depends(deps.now_dep),
    service: SheetSourceService = Depends(deps.get_sheet_source_service),
) -> SheetCheckOut:
    """Read the pasted link and say what is in it, or why it can't be used.
    Saves nothing. Always 200: an unusable sheet is an answer, not an error."""
    return SheetCheckOut.from_check(service.check(payload.url, now=now))


@router.put("", response_model=SheetSourceOut)
def change_sheet(
    payload: SheetCheckRequest,
    actor: Actor = Depends(deps.get_current_actor),
    now: dt.datetime = Depends(deps.now_dep),
    service: SheetSourceService = Depends(deps.get_sheet_source_service),
) -> SheetSourceOut:
    """Use this sheet. Re-checks it; 422 (with the problem's ``code``) if it
    can't be read, and then nothing is saved. The worker imports it within about
    half a minute; ``importing`` is true until it has."""
    state = service.apply(payload.url, actor=actor, now=now)
    return SheetSourceOut.from_state(state, changeable=service.changeable)
