"""Scenario 8: three groups, one fails, retry touches only the failed one.

SI Team and Management succeed; AI Engineering fails. The system must show
2 sent / 1 failed, and retrying must not re-send to the two that already
succeeded -- SENT is a terminal state with no legal transition back out of it
(see domain/approvals/machine.py), so this is also a state-machine proof, not
just a counting exercise.
"""

from __future__ import annotations

import datetime as dt

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from interlock.domain.ports.whatsapp import ErrorClass
from tests.acceptance.conftest import EVENING, run_tick, trigger_reviews


def test_one_group_failing_does_not_affect_the_others_and_retry_is_scoped(
    client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
) -> None:
    clock.set_to(EVENING)

    si_team = client.post(
        "/whatsapp/groups", json={"display_name": "SI Team", "external_jid": "si@g.us"}
    ).json()
    management = client.post(
        "/whatsapp/groups", json={"display_name": "Management", "external_jid": "mgmt@g.us"}
    ).json()
    ai_eng = client.post(
        "/whatsapp/groups", json={"display_name": "AI Engineering", "external_jid": "ai@g.us"}
    ).json()
    whatsapp.given_failure("ai@g.us", code="GROUP_NOT_FOUND", error_class=ErrorClass.PERMANENT)

    review = trigger_reviews(now=EVENING)[0].request
    commit = client.post(
        f"/approval-requests/{review.id}/commit",
        json={
            "mode": "SHARE_ONLY",
            "action_version": 1,
            "recipient_group_ids": [si_team["id"], management["id"], ai_eng["id"]],
        },
    ).json()

    run_tick(whatsapp, now=EVENING + dt.timedelta(seconds=5))

    recipients = {r["whatsapp_group_id"]: r for r in commit["recipients"]}
    assert whatsapp.delivered_count("si@g.us") == 1
    assert whatsapp.delivered_count("mgmt@g.us") == 1
    assert whatsapp.delivered_count("ai@g.us") == 0

    failed_recipient_id = recipients[ai_eng["id"]]["id"]
    whatsapp.clear_failures()

    retry = client.post(f"/shares/recipients/{failed_recipient_id}/retry")
    assert retry.status_code == 200
    assert retry.json()["state"] == "SENT"

    # The two that already succeeded must not have been touched by the retry.
    assert whatsapp.delivered_count("si@g.us") == 1
    assert whatsapp.delivered_count("mgmt@g.us") == 1
    assert whatsapp.delivered_count("ai@g.us") == 1
