"""Scenario 7: a task added from the review panel gets an id, appears
immediately, and is included in the next report.
"""

from __future__ import annotations

import datetime as dt

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import MORNING, run_tick, trigger_reviews


def test_task_added_during_review_gets_an_id_and_is_shared(
    client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
) -> None:
    clock.set_to(MORNING)

    review = trigger_reviews(now=MORNING)[0].request

    created = client.post(
        "/tasks",
        json={"title": "Customer integration follow-up", "priority": "HIGH"},
    )
    assert created.status_code == 201
    task = created.json()
    assert task["display_id"].startswith("TSK-")

    detail = client.get(f"/approval-requests/{review.id}").json()
    assert task["id"] in {t["id"] for t in detail["tasks"]}
    assert detail["summary"]["newly_added"] >= 1

    group = client.post(
        "/whatsapp/groups",
        json={"display_name": "SI Team", "external_jid": "si-team@g.us"},
    ).json()
    commit = client.post(
        f"/approval-requests/{review.id}/commit",
        json={
            "mode": "SHARE_ONLY",
            "action_version": 1,
            "recipient_group_ids": [group["id"]],
        },
    )
    assert "Customer integration follow-up" in commit.json()["preview"]

    run_tick(whatsapp, now=MORNING + dt.timedelta(seconds=5))
    sent_body = whatsapp.sent_to("si-team@g.us")[0].body
    assert "Customer integration follow-up" in sent_body
