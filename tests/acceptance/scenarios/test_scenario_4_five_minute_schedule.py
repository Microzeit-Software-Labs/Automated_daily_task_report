"""Scenario 4: schedule for 5 minutes out; the browser closing changes nothing.

No timer lives in the client -- ``send_at`` is a durable row in
``scheduled_actions``. A tick before the five minutes are up must not send;
a tick after must. "The browser closes" is simulated by simply never calling
the API again between commit and the later tick -- there is nothing else a
browser could have been doing to keep this alive.
"""

from __future__ import annotations

import datetime as dt

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from tests.acceptance.conftest import EVENING, run_tick, trigger_reviews


def test_five_minute_schedule_survives_with_no_client_involvement(
    client: TestClient, whatsapp: MockWhatsAppProvider
) -> None:
    group = client.post(
        "/whatsapp/groups",
        json={"display_name": "SI Team", "external_jid": "si-team@g.us"},
    ).json()
    review = trigger_reviews(now=EVENING)[0].request
    send_at = EVENING + dt.timedelta(minutes=5)

    commit = client.post(
        f"/approval-requests/{review.id}/commit",
        json={
            "mode": "SHARE_ONLY",
            "action_version": 1,
            "recipient_group_ids": [group["id"]],
            "send_at": send_at.isoformat(),
        },
    )
    assert commit.status_code == 200
    assert commit.json()["job"]["state"] == "SCHEDULED"

    # The client (browser) does nothing further from here on.

    too_early = run_tick(whatsapp, now=EVENING + dt.timedelta(minutes=2))
    assert too_early.actions_claimed == 0
    assert whatsapp.delivered_count("si-team@g.us") == 0

    on_time = run_tick(whatsapp, now=send_at + dt.timedelta(seconds=1))
    assert on_time.actions_claimed == 1
    assert on_time.sent == 1
    assert whatsapp.delivered_count("si-team@g.us") == 1
