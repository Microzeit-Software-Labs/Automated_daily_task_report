"""Scenario 9: WhatsApp disconnected must never block task management, and
must never lose data -- the share simply queues for retry instead.
"""

from __future__ import annotations

import datetime as dt

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from interlock.domain.ports.whatsapp import ConnectionState
from tests.acceptance.conftest import EVENING, run_tick, trigger_reviews


def test_whatsapp_down_does_not_block_or_lose_task_updates(
    client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
) -> None:
    clock.set_to(EVENING)
    whatsapp.given_connection(ConnectionState.UNAVAILABLE)

    task = client.post("/tasks", json={"title": "Deploy production server"}).json()
    patched = client.patch(
        f"/tasks/{task['id']}",
        json={"status": "COMPLETED"},
        headers={"If-Match": str(task["version"])},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "COMPLETED"

    readiness = client.get("/readiness")
    assert readiness.status_code == 200  # WhatsApp being down never fails readiness
    assert readiness.json()["whatsapp"]["can_send"] is False

    group = client.post(
        "/whatsapp/groups", json={"display_name": "SI Team", "external_jid": "si@g.us"}
    ).json()
    review = trigger_reviews(now=EVENING)[0].request
    commit = client.post(
        f"/approval-requests/{review.id}/commit",
        json={
            "mode": "SHARE_ONLY",
            "action_version": 1,
            "recipient_group_ids": [group["id"]],
        },
    )
    assert commit.status_code == 200  # saving/scheduling the share still succeeds

    result = run_tick(whatsapp, now=EVENING + dt.timedelta(seconds=5))
    assert result.sent == 0
    assert whatsapp.delivered_count("si@g.us") == 0

    # The task edit made while WhatsApp was down is completely unaffected.
    still_there = client.get(f"/tasks/{task['id']}").json()
    assert still_there["status"] == "COMPLETED"

    # Once WhatsApp recovers, the queued retry goes through with no further
    # user action -- the failure was transient, not a lost report.
    whatsapp.given_connection(ConnectionState.CONNECTED)
    recovered = run_tick(whatsapp, now=EVENING + dt.timedelta(seconds=35))
    assert recovered.sent == 1
    assert whatsapp.delivered_count("si@g.us") == 1
