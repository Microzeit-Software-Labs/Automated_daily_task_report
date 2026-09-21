"""Scenario 3: Share Only sends the current data without touching it.

No task_updates in the commit body at all -- and the task's version must not
move, proving the "current report, no edits" promise is real rather than
merely undocumented.
"""

from __future__ import annotations

import datetime as dt

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import EVENING, run_tick, trigger_reviews


def test_share_only_leaves_task_data_untouched(
    client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
) -> None:
    # See test_scenario_2's identical note: the API's own clock (which
    # /commit uses for its implicit "now") must agree with the EVENING
    # constant this test drives trigger_reviews()/run_tick() with directly.
    clock.set_to(EVENING)

    task = client.post(
        "/tasks", json={"title": "Database migration", "status": "PENDING"}
    ).json()
    group = client.post(
        "/whatsapp/groups",
        json={"display_name": "Management", "external_jid": "mgmt@g.us"},
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
    assert commit.status_code == 200

    unchanged = client.get(f"/tasks/{task['id']}").json()
    assert unchanged["version"] == task["version"]
    assert unchanged["status"] == "PENDING"

    run_tick(whatsapp, now=EVENING + dt.timedelta(seconds=5))
    assert whatsapp.delivered_count("mgmt@g.us") == 1
