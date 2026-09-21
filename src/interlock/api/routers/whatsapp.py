"""WhatsApp group configuration and connection status.

``POST /whatsapp/groups/resolve`` never auto-selects a match -- see
``WhatsAppProvider.resolve_group``'s docstring. A group is only ever added
with a JID a human explicitly picked from the candidate list this endpoint
returns; there is no path that lets a bare display name become a group.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends

from interlock.adapters.persistence.whatsapp_group_repository import WhatsAppGroupRepository
from interlock.api import deps
from interlock.api.schemas import (
    GroupCandidateOut,
    ResolveGroupRequest,
    WhatsAppGroupCreateRequest,
    WhatsAppGroupOut,
    WhatsAppStatusOut,
)
from interlock.domain.ports.whatsapp import WhatsAppProvider

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


@router.get("/groups", response_model=list[WhatsAppGroupOut])
def list_groups(
    enabled_only: bool = False,
    repo: WhatsAppGroupRepository = Depends(deps.get_group_repo),
) -> list[WhatsAppGroupOut]:
    return [WhatsAppGroupOut.from_entity(g) for g in repo.list_groups(enabled_only=enabled_only)]


@router.post("/groups/resolve", response_model=list[GroupCandidateOut])
def resolve_group(
    payload: ResolveGroupRequest,
    whatsapp: WhatsAppProvider = Depends(deps.get_whatsapp),
) -> list[GroupCandidateOut]:
    candidates = whatsapp.resolve_group(payload.name)
    return [
        GroupCandidateOut(
            external_jid=c.external_jid,
            display_name=c.display_name,
            member_count=c.member_count,
            confidence=c.confidence,
        )
        for c in candidates
    ]


@router.post("/groups", response_model=WhatsAppGroupOut, status_code=201)
def add_group(
    payload: WhatsAppGroupCreateRequest,
    now: dt.datetime = Depends(deps.now_dep),
    repo: WhatsAppGroupRepository = Depends(deps.get_group_repo),
) -> WhatsAppGroupOut:
    """Pins the JID the caller supplies -- normally one just returned by
    ``/whatsapp/groups/resolve`` -- permanently. It is never re-resolved by
    name at send time, so a later rename or a second similarly-named group
    cannot silently redirect a report."""
    group = repo.add(
        display_name=payload.display_name,
        external_jid=payload.external_jid,
        now=now,
        description=payload.description,
        default_morning=payload.default_morning,
        default_evening=payload.default_evening,
    )
    return WhatsAppGroupOut.from_entity(group)


@router.patch("/groups/{group_id}/enabled", response_model=WhatsAppGroupOut)
def set_group_enabled(
    group_id: str,
    enabled: bool,
    now: dt.datetime = Depends(deps.now_dep),
    repo: WhatsAppGroupRepository = Depends(deps.get_group_repo),
) -> WhatsAppGroupOut:
    group = repo.set_enabled(group_id, enabled=enabled, at=now)
    return WhatsAppGroupOut.from_entity(group)


@router.get("/status", response_model=WhatsAppStatusOut)
def whatsapp_status(whatsapp: WhatsAppProvider = Depends(deps.get_whatsapp)) -> WhatsAppStatusOut:
    status = whatsapp.health()
    return WhatsAppStatusOut(
        provider=status.provider,
        state=status.state.value,
        checked_at=status.checked_at,
        last_successful_send_at=status.last_successful_send_at,
        can_send=status.can_send,
        detail=status.detail,
    )
