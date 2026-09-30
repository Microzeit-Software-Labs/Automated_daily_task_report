"""Scenario 5: a custom 21:30 send survives a full server restart.

Unlike Scenario 4 (which only needs the worker to keep ticking), this
reconstructs the *entire application* from scratch -- a fresh engine, a fresh
FastAPI app, a fresh TestClient -- to stand in for the process actually being
killed and restarted. Only the database is allowed to carry state across that
boundary, which is the whole point of "Postgres is the schedule of record."
"""

from __future__ import annotations

import datetime as dt

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.api.main import create_app
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import EVENING, _test_settings, run_tick, trigger_reviews


def test_custom_time_send_survives_a_full_restart(
    client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
) -> None:
    # Approval happens at 17:00, four and a half hours before the 21:30 send.
    # Without this the API's clock sits at its default (22:30), *after* the
    # send time, and the test never exercises "approved long before it is due"
    # -- which the missed-window rule once held back as stale.
    clock.set_to(EVENING)
    group = client.post(
        "/whatsapp/groups",
        json={"display_name": "SI Team", "external_jid": "si-team@g.us"},
    ).json()
    review = trigger_reviews(now=EVENING)[0].request
    send_at = EVENING.astimezone(dt.UTC).replace(hour=16, minute=0)  # 21:30 IST

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

    # --- "the server restarts" ------------------------------------------
    # A brand new engine, app and clock -- nothing from the client fixture
    # above is reused. Only what is actually in Postgres survives.
    restarted_clock = FrozenClock(send_at)
    restarted_whatsapp = MockWhatsAppProvider(restarted_clock)
    restarted_app = create_app(
        settings=_test_settings(), clock=restarted_clock, whatsapp_provider=restarted_whatsapp
    )
    with TestClient(restarted_app) as restarted_client:
        # The review and job created before the "restart" are still visible.
        still_there = restarted_client.get(f"/approval-requests/{review.id}")
        assert still_there.status_code == 200
        assert still_there.json()["request"]["state"] == "APPROVED"

    result = run_tick(whatsapp, now=send_at + dt.timedelta(seconds=1))
    assert result.actions_claimed == 1
    assert result.sent == 1
    assert whatsapp.delivered_count("si-team@g.us") == 1

    final_job = client.get(f"/approval-requests/{review.id}").json()
    assert final_job["request"]["state"] == "APPROVED"  # request state is unaffected by job state
