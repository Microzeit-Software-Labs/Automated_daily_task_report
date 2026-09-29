"""build_whatsapp_provider's gate -- mock by default, local_agent only with
the ToS ack, anything else rejected loudly."""

from __future__ import annotations

import pytest

from interlock.adapters.whatsapp.factory import build_whatsapp_provider
from interlock.adapters.whatsapp.local_agent import LocalAgentProvider
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.config import Settings, WhatsAppProviderName
from interlock.domain.common.clock import FrozenClock
from interlock.domain.ports.whatsapp import WhatsAppProvider
from tests.conftest import BRIEF_EVENING


class TestMock:
    def test_defaults_to_the_mock(self) -> None:
        settings = Settings()
        provider = build_whatsapp_provider(
            settings, clock=FrozenClock(BRIEF_EVENING), session_factory=None  # type: ignore[arg-type]
        )
        assert isinstance(provider, MockWhatsAppProvider)


class TestLocalAgent:
    def test_refuses_to_start_without_the_tos_ack(self) -> None:
        settings = Settings(
            whatsapp_provider=WhatsAppProviderName.LOCAL_AGENT,
            whatsapp_local_agent_tos_ack=False,
        )
        with pytest.raises(RuntimeError, match="WHATSAPP_LOCAL_AGENT_TOS_ACK"):
            build_whatsapp_provider(
                settings,
                clock=FrozenClock(BRIEF_EVENING),
                session_factory=None,  # type: ignore[arg-type]
            )

    def test_builds_a_local_agent_provider_once_acked(self) -> None:
        settings = Settings(
            whatsapp_provider=WhatsAppProviderName.LOCAL_AGENT,
            whatsapp_local_agent_tos_ack=True,
        )
        provider = build_whatsapp_provider(
            settings,
            clock=FrozenClock(BRIEF_EVENING),
            session_factory=None,  # type: ignore[arg-type]
        )
        assert isinstance(provider, LocalAgentProvider)
        assert isinstance(provider, WhatsAppProvider)


class TestUnknown:
    def test_cloud_api_is_not_implemented_yet(self) -> None:
        settings = Settings(whatsapp_provider=WhatsAppProviderName.CLOUD_API)
        with pytest.raises(NotImplementedError):
            build_whatsapp_provider(
                settings,
                clock=FrozenClock(BRIEF_EVENING),
                session_factory=None,  # type: ignore[arg-type]
            )
