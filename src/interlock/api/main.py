"""The composition root: builds the FastAPI app and wires every dependency.

This is the one place that decides which concrete adapter backs each port --
``WhatsAppProvider`` is the mock by default, per ``WHATSAPP_PROVIDER=mock``.
Swapping to a real provider later is a change here, not a change to anything
that depends on the port.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI

from interlock.adapters.persistence.base import make_engine, make_session_factory
from interlock.adapters.rendering.table_image import PillowTableRenderer
from interlock.adapters.sheets.factory import build_sheet_reader, build_sheets_provider
from interlock.adapters.whatsapp.factory import build_whatsapp_provider
from interlock.api.errors import register_error_handlers
from interlock.api.idempotency import IdempotencyMiddleware
from interlock.api.routers import (
    approvals,
    health,
    notifications,
    shares,
    sheet,
    sync,
    tasks,
    ui,
    whatsapp,
)
from interlock.config import ReportFormat, Settings, get_settings
from interlock.domain.common.clock import Clock, SystemClock
from interlock.domain.ports.sheets import SpreadsheetProvider
from interlock.domain.ports.whatsapp import WhatsAppProvider
from interlock.domain.sync.sheet_source import SheetLink


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    whatsapp_provider: WhatsAppProvider | None = None,
    sheets_provider: SpreadsheetProvider | None = None,
    sheet_reader: Callable[[SheetLink], str] | None = None,
) -> FastAPI:
    """``clock``, ``whatsapp_provider`` and ``sheets_provider`` are overridable
    so acceptance tests can drive the API against a ``FrozenClock``, a
    ``MockWhatsAppProvider`` and a ``FakeSpreadsheetProvider`` they hold a
    reference to -- production always takes the defaults below. ``sheet_reader``
    stands in for the real fetch of a Google Sheet's CSV, so tests and demos never
    touch the network."""
    settings = settings or get_settings()

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    clock = clock or SystemClock()

    whatsapp_provider = whatsapp_provider or build_whatsapp_provider(
        settings, clock=clock, session_factory=session_factory
    )
    sheets_provider = sheets_provider or build_sheets_provider(settings)

    app = FastAPI(title="Interlock API", version="0.1.0")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.whatsapp = whatsapp_provider
    app.state.sheets = sheets_provider
    # None while the two-way sync owns the sheet: the link can't be changed then.
    app.state.sheet_reader = sheet_reader or build_sheet_reader(settings)
    # Fonts load once here, not per request.
    app.state.image_renderer = (
        PillowTableRenderer() if settings.report_format is ReportFormat.IMAGE else None
    )

    # Starlette 1.6.0's _MiddlewareFactory Protocol checks add_middleware's
    # callable shape structurally against a bare ASGI callable and does not
    # perfectly accommodate the classic BaseHTTPMiddleware subclassing idiom
    # used here. Verified safe at runtime: this exact wiring is what every
    # TestClient request in tests/acceptance/ passes through successfully.
    app.add_middleware(
        IdempotencyMiddleware,  # type: ignore[arg-type]
        session_factory=session_factory,
        clock=clock,
        ttl_hours=settings.idempotency_key_ttl_hours,
    )

    register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(approvals.router)
    app.include_router(whatsapp.router)
    app.include_router(shares.router)
    app.include_router(sync.router)
    app.include_router(sheet.router)
    app.include_router(notifications.router)
    app.include_router(ui.router)
    ui.mount_web_app(app)

    return app


app = create_app()
