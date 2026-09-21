"""Liveness and readiness.

WhatsApp availability is reported here but never gates readiness -- a dead
WhatsApp session must never stop task management from working (Scenario 9).
Readiness checks only what the application cannot function without at all:
the database. The HTTP status code itself carries the verdict (200 ready,
503 not), matching what an orchestrator's health check expects to see rather
than making it parse the body to find out.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from interlock.api import deps
from interlock.domain.ports.whatsapp import WhatsAppProvider

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readiness")
def readiness(
    session: Session = Depends(deps.get_session),
    whatsapp: WhatsAppProvider = Depends(deps.get_whatsapp),
) -> JSONResponse:
    db_ok = True
    db_detail = "connected"
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_detail = str(exc)

    whatsapp_status = whatsapp.health()

    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "ready": db_ok,
            "database": {"ok": db_ok, "detail": db_detail},
            "whatsapp": {
                "state": whatsapp_status.state.value,
                "can_send": whatsapp_status.can_send,
            },
        },
    )
