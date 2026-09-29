"""The Google Sheets sync's one human-facing surface: parked conflicts.

Nothing here triggers a tick -- see ``adapters/sheets/factory.py`` and
``workers/loop.py`` for why that is deliberately the worker's job, never a
browser's, the same reasoning ``tests/acceptance/conftest.py::trigger_reviews``
documents for the review trigger.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends

from interlock.adapters.persistence.sync_repository import SyncRepository
from interlock.api import deps
from interlock.api.schemas import ResolveConflictRequest, SyncConflictOut
from interlock.domain.common.actor import Actor
from interlock.services.sheet_sync_service import SheetSyncService

router = APIRouter(prefix="/sync", tags=["sync"])


@router.get("/conflicts", response_model=list[SyncConflictOut])
def list_conflicts(
    open_only: bool = True,
    sync_repo: SyncRepository = Depends(deps.get_sync_repo),
) -> list[SyncConflictOut]:
    conflicts = sync_repo.list_conflicts(open_only=open_only)
    return [SyncConflictOut.from_entity(c) for c in conflicts]


@router.post("/conflicts/{conflict_id}/resolve", response_model=SyncConflictOut)
def resolve_conflict(
    conflict_id: str,
    payload: ResolveConflictRequest,
    actor: Actor = Depends(deps.get_current_actor),
    now: dt.datetime = Depends(deps.now_dep),
    service: SheetSyncService = Depends(deps.get_sheet_sync_service),
) -> SyncConflictOut:
    conflict = service.resolve_conflict(
        conflict_id, resolution=payload.resolution, resolved_by=str(actor), now=now
    )
    return SyncConflictOut.from_entity(conflict)
