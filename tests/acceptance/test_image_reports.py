"""Image-format reports through the real API: preview, freeze, send, view.

The central promise, checked end to end: the PNG the worker hands to WhatsApp
is byte-for-byte the one frozen at approval and served back afterwards.
"""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Iterator

import pytest
from PIL import Image
from starlette.testclient import TestClient

from interlock.adapters.sheets.fake import FakeSpreadsheetProvider
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.api.main import create_app
from interlock.config import ReportFormat
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import EVENING, _test_settings, run_tick, trigger_reviews


@pytest.fixture
def image_client(
    clean_db: None,
    clock: FrozenClock,
    whatsapp: MockWhatsAppProvider,
    sheets: FakeSpreadsheetProvider,
) -> Iterator[TestClient]:
    settings = _test_settings().model_copy(update={"report_format": ReportFormat.IMAGE})
    app = create_app(
        settings=settings, clock=clock, whatsapp_provider=whatsapp, sheets_provider=sheets
    )
    with TestClient(app) as test_client:
        yield test_client


def _setup(client: TestClient) -> tuple[str, str]:
    client.post("/tasks", json={"title": "Eldorado C2V extraction", "status": "IN_PROGRESS"})
    group = client.post(
        "/whatsapp/groups", json={"display_name": "Test", "external_jid": "test@g.us"}
    ).json()
    review = trigger_reviews(now=EVENING)[-1].request
    return review.id, group["id"]


def test_preview_is_a_caption_plus_a_png(image_client: TestClient, clock: FrozenClock) -> None:
    clock.set_to(EVENING)
    review_id, _ = _setup(image_client)

    preview = image_client.post(f"/approval-requests/{review_id}/preview").json()
    assert preview["has_image"] is True
    assert preview["rendered_body"].startswith("*End of Day Task Update*")

    png = image_client.get(f"/approval-requests/{review_id}/preview.png")
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(png.content)).format == "PNG"


def test_the_sent_image_is_the_frozen_one(
    image_client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
) -> None:
    clock.set_to(EVENING)
    review_id, group_id = _setup(image_client)
    commit = image_client.post(
        f"/approval-requests/{review_id}/commit",
        json={"mode": "SHARE_ONLY", "action_version": 1, "recipient_group_ids": [group_id]},
    )
    assert commit.status_code == 200

    # A task changes after approval: the frozen image must not.
    image_client.post("/tasks", json={"title": "Added after approval"})
    run_tick(whatsapp, now=EVENING + dt.timedelta(seconds=5))

    [sent] = whatsapp.sent
    assert sent.external_jid == "test@g.us"
    assert sent.body == commit.json()["preview"]
    assert sent.image_png is not None

    [share] = image_client.get(f"/approval-requests/{review_id}").json()["shares"]
    assert share["has_image"] is True
    served = image_client.get(f"/shares/snapshots/{share['snapshot_id']}/image.png")
    assert served.status_code == 200
    assert served.content == sent.image_png


def test_text_format_has_no_image(client: TestClient, clock: FrozenClock) -> None:
    clock.set_to(EVENING)
    review_id, _ = _setup(client)

    preview = client.post(f"/approval-requests/{review_id}/preview").json()
    assert preview["has_image"] is False
    assert client.get(f"/approval-requests/{review_id}/preview.png").status_code == 404


def test_unknown_snapshot_image_is_404(image_client: TestClient) -> None:
    assert image_client.get("/shares/snapshots/nope/image.png").status_code == 404
