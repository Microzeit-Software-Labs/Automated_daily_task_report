"""Scenario 1: Update Only never sends anything.

At 09:00, the review appears. The user edits a task and clicks Update Only.
The task change is saved. No WhatsApp message is sent -- not "not sent yet",
genuinely never, because Update Only never creates a share job at all.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from tests.acceptance.conftest import MORNING, trigger_reviews


def test_update_only_saves_the_task_and_sends_nothing(
    client: TestClient, whatsapp: MockWhatsAppProvider
) -> None:
    created = client.post(
        "/tasks", json={"title": "Deploy production server", "status": "PENDING"}
    )
    assert created.status_code == 201
    task = created.json()

    reviews = trigger_reviews(now=MORNING)
    assert len(reviews) == 1
    review_id = reviews[0].request.id

    response = client.post(
        f"/approval-requests/{review_id}/commit",
        json={
            "mode": "UPDATE_ONLY",
            "action_version": 1,
            "task_updates": [
                {
                    "task_id": task["id"],
                    "changes": {"status": "COMPLETED"},
                    "expected_version": task["version"],
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()

    assert body["job"] is None
    assert body["recipients"] == []

    updated_task = client.get(f"/tasks/{task['id']}").json()
    assert updated_task["status"] == "COMPLETED"
    assert updated_task["version"] == task["version"] + 1

    review = client.get(f"/approval-requests/{review_id}").json()
    assert review["request"]["state"] == "CLOSED_NO_SHARE"

    assert whatsapp.sent == ()
