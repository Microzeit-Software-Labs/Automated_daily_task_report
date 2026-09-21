"""The composition root: builds the FastAPI app and wires every dependency.

This is the one place that decides which concrete adapter backs each port --
``WhatsAppProvider`` is the mock in Phase 1, per ``WHATSAPP_PROVIDER=mock``.
Swapping to a real provider later is a change here, not a change to anything
that depends on the port.
"""

from __future__ import annotations

from fastapi import FastAPI

from interlock.adapters.persistence.base import make_engine, make_session_factory
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.api.errors import register_error_handlers
from interlock.api.idempotency import IdempotencyMiddleware
from interlock.api.routers import approvals, health, shares, tasks, whatsapp
from interlock.config import Settings, WhatsAppProviderName, get_settings
from interlock.domain.common.clock import Clock, SystemClock
from interlock.domain.ports.whatsapp import WhatsAppProvider


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    whatsapp_provider: WhatsAppProvider | None = None,
) -> FastAPI:
    """``clock`` and ``whatsapp_provider`` are overridable so acceptance tests
    can drive the API against a ``FrozenClock`` and a ``MockWhatsAppProvider``
    they hold a reference to -- production always takes the defaults below."""
    settings = settings or get_settings()

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    clock = clock or SystemClock()

    if whatsapp_provider is None:
        if settings.whatsapp_provider is not WhatsAppProviderName.MOCK:
            # Phase 1 ships only the mock -- see the Phase 0 architecture's
            # WhatsAppProvider port for what local_agent / cloud_api need
            # before they can be wired in here. Failing loudly beats silently
            # running the mock under a config that claims otherwise.
            raise NotImplementedError(
                f"WHATSAPP_PROVIDER={settings.whatsapp_provider.value!r} is not "
                "implemented in Phase 1. Only 'mock' is available until the local "
                "agent (Phase 3) ships."
            )
        whatsapp_provider = MockWhatsAppProvider(clock)

    app = FastAPI(title="Interlock API", version="0.1.0")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.whatsapp = whatsapp_provider

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

    return app


app = create_app()
