"""Builds the configured :class:`WhatsAppProvider`.

The one place that decides mock vs. local agent vs. (eventually) cloud API,
shared by both composition roots (``api/main.py`` and ``workers/loop.py``) so
they cannot pick different providers for the same ``WHATSAPP_PROVIDER``
setting -- mirrors ``adapters/sheets/factory.py``'s role for the Sheets sync.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session, sessionmaker

from interlock.adapters.whatsapp.local_agent import LocalAgentProvider
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.config import Settings, WhatsAppProviderName
from interlock.domain.common.clock import Clock
from interlock.domain.ports.whatsapp import WhatsAppProvider


def build_whatsapp_provider(
    settings: Settings,
    *,
    clock: Clock,
    session_factory: sessionmaker[Session],
) -> WhatsAppProvider:
    if settings.whatsapp_provider is WhatsAppProviderName.MOCK:
        return MockWhatsAppProvider(clock)

    if settings.whatsapp_provider is WhatsAppProviderName.LOCAL_AGENT:
        if not settings.whatsapp_local_agent_tos_ack:
            # Fails loudly rather than silently degrading to the mock, same
            # pattern build_sheets_provider uses for missing Google
            # credentials -- but this gate is about a knowing risk decision,
            # not merely a missing credential, so it needs an explicit ack
            # rather than just a present/absent value.
            raise RuntimeError(
                "WHATSAPP_PROVIDER=local_agent requires WHATSAPP_LOCAL_AGENT_TOS_ACK=true. "
                "This delivery path uses Baileys, an unofficial WhatsApp client library "
                "that violates WhatsApp's Terms of Service and carries a real risk of the "
                "linked number being banned. Set the flag only after reading "
                "docs/whatsapp-agent-setup.md and accepting that risk knowingly."
            )
        return LocalAgentProvider(
            session_factory=session_factory,
            clock=clock,
            poll_interval=dt.timedelta(milliseconds=settings.whatsapp_agent_poll_interval_ms),
            command_timeout=dt.timedelta(
                seconds=settings.whatsapp_agent_command_timeout_seconds
            ),
            heartbeat_stale_after=dt.timedelta(
                seconds=settings.whatsapp_agent_heartbeat_stale_seconds
            ),
        )

    raise NotImplementedError(
        f"WHATSAPP_PROVIDER={settings.whatsapp_provider.value!r} is not implemented."
    )
