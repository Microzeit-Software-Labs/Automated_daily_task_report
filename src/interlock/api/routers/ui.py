"""What the browser UI needs besides the domain endpoints: its settings, and
the built single-page app itself.

The UI (``apps/web``) is a static Vite build served from this same process at
``/app/``, so there is no fourth process to keep alive and no CORS to
configure. When it hasn't been built, ``/`` falls back to ``/docs``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from interlock.api import deps
from interlock.api.schemas import UiConfigOut
from interlock.config import Settings
from interlock.services.sheet_source_service import SheetSourceService

WEB_DIST = Path(__file__).resolve().parents[4] / "apps" / "web" / "dist"

router = APIRouter(tags=["ui"])


@router.get("/config/ui", response_model=UiConfigOut)
def ui_config(
    settings: Settings = Depends(deps.get_settings_dep),
    sheets: SheetSourceService = Depends(deps.get_sheet_source_service),
    now: dt.datetime = Depends(deps.now_dep),
) -> UiConfigOut:
    sheet = sheets.current(now=now)
    return UiConfigOut(
        timezone=settings.timezone,
        morning_alert_time=settings.morning_alert_time,
        evening_alert_time=settings.evening_alert_time,
        working_days=sorted(settings.working_days),
        allow_custom_send_time=settings.allow_custom_send_time,
        sheet_url=sheet.url if sheet else None,
        user_name=settings.default_user_name,
    )


def mount_web_app(app: FastAPI, dist: Path = WEB_DIST) -> None:
    built = (dist / "index.html").is_file()
    if built:
        app.mount("/app", StaticFiles(directory=dist, html=True), name="web")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/app/" if built else "/docs")
