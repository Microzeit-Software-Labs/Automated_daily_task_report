"""WhatsApp group configuration and connection status.

``POST /whatsapp/groups/resolve`` never auto-selects a match -- see
``WhatsAppProvider.resolve_group``'s docstring. A group is only ever added
with a JID a human explicitly picked from the candidate list this endpoint
returns; there is no path that lets a bare display name become a group.
"""

from __future__ import annotations

import datetime as dt
import io

import segno
from fastapi import APIRouter, Depends, Response

from interlock.adapters.persistence.whatsapp_group_repository import WhatsAppGroupRepository
from interlock.api import deps
from interlock.api.schemas import (
    GroupCandidateOut,
    LinkStatusOut,
    ResolveGroupRequest,
    WhatsAppGroupCreateRequest,
    WhatsAppGroupOut,
    WhatsAppStatusOut,
)
from interlock.domain.common.errors import NotFoundError, ProviderUnavailableError
from interlock.domain.ports.whatsapp import WhatsAppLinking, WhatsAppProvider

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
    status = whatsapp.health()
    if not status.can_send:
        # Say so now, instead of waiting out a 20-second timeout for an agent
        # that cannot answer.
        raise ProviderUnavailableError(
            "WhatsApp isn't connected, so your groups can't be looked up. "
            "Reconnect it first (Settings > WhatsApp).",
            state=status.state.value,
            reason=status.reason,
        )
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
    return WhatsAppStatusOut.from_status(whatsapp.health())


# -- linking a phone ---------------------------------------------------------
#
# Scanning a QR code is the only way to link, change or re-link a phone. The
# current link keeps working, and scheduled reports keep sending, for the whole
# time an attempt is in progress: the agent pairs in a scratch session and only
# switches over once the new phone has fully linked.


@router.get("/link", response_model=LinkStatusOut)
def link_status(linking: WhatsAppLinking = Depends(deps.get_linking)) -> LinkStatusOut:
    return LinkStatusOut.from_status(linking.link_status())


@router.post("/link", response_model=LinkStatusOut)
def start_link(linking: WhatsAppLinking = Depends(deps.get_linking)) -> LinkStatusOut:
    """Start linking a phone (or start over, if an attempt is already running)."""
    return LinkStatusOut.from_status(linking.start_link())


@router.delete("/link", response_model=LinkStatusOut)
def cancel_link(linking: WhatsAppLinking = Depends(deps.get_linking)) -> LinkStatusOut:
    """Abandon the attempt. The current link is untouched."""
    return LinkStatusOut.from_status(linking.cancel_link())


@router.get(
    "/link/qr.svg",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}}},
)
def link_qr(linking: WhatsAppLinking = Depends(deps.get_linking)) -> Response:
    """The current QR code. It changes about every 20 seconds; ``qr_version`` on
    ``GET /whatsapp/link`` changes with it."""
    payload = linking.link_status().qr
    if payload is None:
        raise NotFoundError("There is no QR code to show right now.")
    buffer = io.BytesIO()
    # Dark on an explicit white: a transparent background would be unreadable
    # (and unscannable) against the UI's dark theme.
    segno.make(payload, error="m", micro=False).save(
        buffer, kind="svg", scale=8, border=3, dark="#000000", light="#ffffff"
    )
    return Response(
        content=buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/reconnect", status_code=204)
def reconnect(linking: WhatsAppLinking = Depends(deps.get_linking)) -> Response:
    """Reconnect a link that is still valid (dropped, or taken over by another
    session). A logged-out link needs a new QR instead."""
    linking.reconnect()
    return Response(status_code=204)
