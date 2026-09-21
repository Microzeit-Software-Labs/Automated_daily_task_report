"""Scenario 2: edit a task, mark it completed, Update + Share to SI Team.

The preview must reflect the edit, the send must not happen until the
scheduler actually ticks (nothing sends synchronously inside /commit), and
once it does, WhatsApp must show exactly one delivery to SI Team.
"""

from __future__ import annotations

import datetime as dt

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import MORNING, run_tick, trigger_reviews


def test_update_and_share_sends_after_the_scheduler_ticks(
    client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
) -> None:
    # The API's own clock (used for /commit's implicit "now", which becomes
    # a job's run_at when no explicit send_at is given) must agree with the
    # MORNING/EVENING constants this test drives the standalone
    # trigger_reviews()/run_tick() helpers with -- those two are otherwise
    # entirely independent clocks with no reason to already match.
    clock.set_to(MORNING)

    task = client.post(
        "/tasks", json={"title": "Deploy customer server", "status": "PENDING"}
    ).json()

    group = client.post(
        "/whatsapp/groups",
        json={"display_name": "SI Team", "external_jid": "si-team@g.us"},
    ).json()

    review = trigger_reviews(now=MORNING)[0].request

    commit = client.post(
        f"/approval-requests/{review.id}/commit",
        json={
            "mode": "UPDATE_AND_SHARE",
            "action_version": 1,
            "task_updates": [
                {
                    "task_id": task["id"],
                    "changes": {"status": "COMPLETED"},
                    "expected_version": task["version"],
                }
            ],
            "recipient_group_ids": [group["id"]],
        },
    )
    assert commit.status_code == 200
    body = commit.json()
    assert body["job"]["state"] == "SCHEDULED"
    assert "Deploy customer server" in body["preview"]
    assert whatsapp.sent == ()  # nothing sent synchronously by /commit itself

    approved = client.get(f"/approval-requests/{review.id}").json()
    assert approved["request"]["state"] == "APPROVED"

    run_tick(whatsapp, now=MORNING + dt.timedelta(seconds=5))

    assert whatsapp.delivered_count("si-team@g.us") == 1
    sent_body = whatsapp.sent_to("si-team@g.us")[0].body
    assert "Deploy customer server" in sent_body
