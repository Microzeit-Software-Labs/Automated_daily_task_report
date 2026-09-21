"""Scenario 6: clicking Update + Share twice must send exactly once.

The client sends the same action_version both times, exactly as a real
double-click would (the button's action_version does not change just because
the user clicked it twice). The second call must be recognised as a replay,
not executed again, and the scheduler must still only ever send one message.
"""

from __future__ import annotations

import datetime as dt

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import EVENING, run_tick, trigger_reviews


def test_double_click_sends_exactly_one_message(
    client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
) -> None:
    clock.set_to(EVENING)

    group = client.post(
        "/whatsapp/groups",
        json={"display_name": "SI Team", "external_jid": "si-team@g.us"},
    ).json()
    review = trigger_reviews(now=EVENING)[0].request

    commit_body = {
        "mode": "SHARE_ONLY",
        "action_version": 1,
        "recipient_group_ids": [group["id"]],
    }

    first = client.post(f"/approval-requests/{review.id}/commit", json=commit_body)
    second = client.post(f"/approval-requests/{review.id}/commit", json=commit_body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert not first.json()["already_committed"]
    assert second.json()["already_committed"]
    assert first.json()["job"]["id"] == second.json()["job"]["id"]

    run_tick(whatsapp, now=EVENING + dt.timedelta(seconds=5))

    assert whatsapp.delivered_count("si-team@g.us") == 1  # not two
    assert whatsapp.send_attempts >= 1  # the replay may re-attempt; delivery must not duplicate
