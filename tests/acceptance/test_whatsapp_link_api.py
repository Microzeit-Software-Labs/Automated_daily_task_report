"""Linking, relinking and reconnecting WhatsApp from the UI, through the real
API. The mock provider plays the agent; ``complete_link`` plays the phone
scanning the QR.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from collections.abc import Sequence

from starlette.testclient import TestClient

from interlock.adapters.sheets.fake import FakeSpreadsheetProvider
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.api.main import create_app
from interlock.domain.common.clock import FrozenClock
from interlock.domain.ports.whatsapp import (
    ConnectionState,
    GroupCandidate,
    ProviderStatus,
    SendOutcome,
)
from tests.acceptance.conftest import T0, _test_settings


class TestStatus:
    def test_connected_reports_the_linked_phone_as_a_number(
        self, client: TestClient, whatsapp: MockWhatsAppProvider
    ) -> None:
        whatsapp.complete_link(jid="919876543210:12@s.whatsapp.net", name="Kaif")

        status = client.get("/whatsapp/status").json()

        assert status["state"] == "CONNECTED"
        assert status["can_send"] is True
        assert status["reason"] is None
        assert status["account_number"] == "+919876543210"
        assert status["account_name"] == "Kaif"

    def test_a_logged_out_link_says_why(
        self, client: TestClient, whatsapp: MockWhatsAppProvider
    ) -> None:
        whatsapp.given_link_problem("LOGGED_OUT")

        status = client.get("/whatsapp/status").json()

        assert status["state"] == "LOGIN_REQUIRED"
        assert status["can_send"] is False
        assert status["reason"] == "LOGGED_OUT"

    def test_a_taken_over_link_is_unavailable_not_a_relink(
        self, client: TestClient, whatsapp: MockWhatsAppProvider
    ) -> None:
        whatsapp.given_link_problem("REPLACED")

        status = client.get("/whatsapp/status").json()

        assert status["state"] == "UNAVAILABLE"
        assert status["reason"] == "REPLACED"


class TestLinkingAPhone:
    def test_nothing_is_happening_by_default(self, client: TestClient) -> None:
        link = client.get("/whatsapp/link").json()

        assert link["state"] == "IDLE"
        assert link["has_qr"] is False
        assert link["agent_online"] is True

    def test_start_offers_a_qr_code(self, client: TestClient) -> None:
        link = client.post("/whatsapp/link").json()

        assert link["state"] == "WAITING_FOR_SCAN"
        assert link["has_qr"] is True
        assert link["pairing_id"]
        assert link["qr_version"]

    def test_the_qr_is_a_real_scannable_svg_on_white(self, client: TestClient) -> None:
        client.post("/whatsapp/link")

        response = client.get("/whatsapp/link/qr.svg")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")
        assert response.headers["cache-control"] == "no-store"
        root = ET.fromstring(response.content)
        assert root.tag.endswith("svg")
        # A white ground covering the whole image (quiet zone included) under
        # black modules: a transparent background would be unreadable, and
        # unscannable, against the UI's dark theme. (segno writes #fff / #000.)
        ns = "{http://www.w3.org/2000/svg}"
        background = [p for p in root.iter(f"{ns}path") if p.get("fill") == "#fff"]
        modules = [p for p in root.iter(f"{ns}path") if p.get("stroke") == "#000"]
        assert len(background) == 1 and len(modules) == 1

    def test_a_rotated_qr_is_a_different_image_with_a_new_version(
        self, client: TestClient, whatsapp: MockWhatsAppProvider, clock: FrozenClock
    ) -> None:
        first_link = client.post("/whatsapp/link").json()
        first_svg = client.get("/whatsapp/link/qr.svg").content

        clock.advance(dt.timedelta(seconds=20))
        whatsapp.advance_qr()
        second_link = client.get("/whatsapp/link").json()
        second_svg = client.get("/whatsapp/link/qr.svg").content

        assert second_link["qr_version"] != first_link["qr_version"]
        assert second_svg != first_svg

    def test_no_qr_image_when_there_is_nothing_to_scan(self, client: TestClient) -> None:
        response = client.get("/whatsapp/link/qr.svg")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_scanning_connects_the_new_phone(
        self, client: TestClient, whatsapp: MockWhatsAppProvider
    ) -> None:
        whatsapp.given_link_problem("NOT_LINKED")
        client.post("/whatsapp/link")

        whatsapp.complete_link(jid="911234567890:3@s.whatsapp.net", name="New phone")

        link = client.get("/whatsapp/link").json()
        status = client.get("/whatsapp/status").json()
        assert link["state"] == "SUCCEEDED"
        assert link["has_qr"] is False
        assert status["state"] == "CONNECTED"
        assert status["account_number"] == "+911234567890"
        assert status["reason"] is None
        assert client.get("/whatsapp/link/qr.svg").status_code == 404

    def test_cancelling_abandons_the_attempt(self, client: TestClient) -> None:
        client.post("/whatsapp/link")

        link = client.delete("/whatsapp/link").json()

        assert link["state"] == "CANCELLED"
        assert link["has_qr"] is False

    def test_cancelling_when_nothing_is_running_is_harmless(self, client: TestClient) -> None:
        link = client.delete("/whatsapp/link").json()

        assert link["state"] == "IDLE"

    def test_an_expired_qr_is_reported_and_can_be_replaced(
        self, client: TestClient, whatsapp: MockWhatsAppProvider
    ) -> None:
        client.post("/whatsapp/link")
        whatsapp.expire_link()
        assert client.get("/whatsapp/link").json()["state"] == "EXPIRED"

        again = client.post("/whatsapp/link").json()

        assert again["state"] == "WAITING_FOR_SCAN"
        assert again["has_qr"] is True

    def test_relinking_while_connected_keeps_the_current_link_working_until_it_completes(
        self, client: TestClient, whatsapp: MockWhatsAppProvider
    ) -> None:
        whatsapp.complete_link(jid="919876543210:12@s.whatsapp.net", name="Old phone")

        client.post("/whatsapp/link")  # "Link a different phone"

        status = client.get("/whatsapp/status").json()
        assert status["can_send"] is True
        assert status["account_number"] == "+919876543210"


class TestReconnect:
    def test_reconnects_a_link_that_is_still_valid(
        self, client: TestClient, whatsapp: MockWhatsAppProvider
    ) -> None:
        whatsapp.given_link_problem("REPLACED")

        response = client.post("/whatsapp/reconnect")

        assert response.status_code == 204
        assert client.get("/whatsapp/status").json()["state"] == "CONNECTED"

    def test_a_link_that_does_not_exist_cannot_be_reconnected(
        self, client: TestClient, whatsapp: MockWhatsAppProvider
    ) -> None:
        whatsapp.given_link_problem("NOT_LINKED")

        response = client.post("/whatsapp/reconnect")

        assert response.status_code == 503
        assert "QR code" in response.json()["error"]["message"]


class TestGroupLookupNeedsAConnection:
    def test_fails_straight_away_with_a_clear_message_when_disconnected(
        self, client: TestClient, whatsapp: MockWhatsAppProvider
    ) -> None:
        whatsapp.given_connection(ConnectionState.UNAVAILABLE)

        response = client.post("/whatsapp/groups/resolve", json={"name": "Test"})

        assert response.status_code == 503
        assert "isn't connected" in response.json()["error"]["message"]

    def test_works_when_connected(self, client: TestClient, whatsapp: MockWhatsAppProvider) -> None:
        whatsapp.given_group("Test", "test@g.us", members=2)

        response = client.post("/whatsapp/groups/resolve", json={"name": "Test"})

        assert response.status_code == 200
        assert response.json()[0]["external_jid"] == "test@g.us"


class _SendOnlyProvider:
    """A provider with no way to be linked from the UI (like the official API)."""

    name = "send-only"

    def health(self) -> ProviderStatus:
        return ProviderStatus(
            state=ConnectionState.CONNECTED, checked_at=T0, provider=self.name, detail="ok"
        )

    def resolve_group(self, name: str) -> Sequence[GroupCandidate]:
        return ()

    def send_text(
        self,
        *,
        external_jid: str,
        body: str,
        client_message_id: str,
        image_png: bytes | None = None,
    ) -> SendOutcome:
        return SendOutcome.success(client_message_id=client_message_id, provider_message_id="x")

    def delivery_state(self, client_message_id: str) -> SendOutcome | None:
        return None


def test_a_provider_that_cannot_be_linked_says_so(
    clean_db: None, clock: FrozenClock, sheets: FakeSpreadsheetProvider
) -> None:
    app = create_app(
        settings=_test_settings(),
        clock=clock,
        whatsapp_provider=_SendOnlyProvider(),
        sheets_provider=sheets,
    )
    with TestClient(app) as client:
        for request in (
            lambda: client.get("/whatsapp/link"),
            lambda: client.post("/whatsapp/link"),
            lambda: client.post("/whatsapp/reconnect"),
        ):
            response = request()
            assert response.status_code == 503
            assert "can't be linked" in response.json()["error"]["message"]

        # Status still works: it is part of every provider.
        assert client.get("/whatsapp/status").json()["state"] == "CONNECTED"
