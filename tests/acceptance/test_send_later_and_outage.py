"""Scheduled sends and WhatsApp outages, end to end.

Two promises the popup ("In 5 minutes" / "Custom time") and the reconnect
flow both rest on:

* a report the user scheduled for later goes out *at that time*, however long
  after approval that is -- it is judged from when it was meant to go out, not
  from when it was approved;
* while WhatsApp is down, due reports wait (no attempts burned) and go out on
  reconnect -- unless it is by then too late, in which case they are held back
  for a human, and say why.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from interlock.domain.ports.whatsapp import ConnectionState, ErrorClass
from tests.acceptance.conftest import EVENING, run_tick, trigger_reviews

HOUR = dt.timedelta(hours=1)
MINUTE = dt.timedelta(minutes=1)
SECOND = dt.timedelta(seconds=1)


def _approve(client: TestClient, *, send_at: dt.datetime | None = None) -> str:
    """Approve today's evening review to one group; return the review id."""
    group = client.post(
        "/whatsapp/groups", json={"display_name": "Test", "external_jid": "test@g.us"}
    ).json()
    review = trigger_reviews(now=EVENING)[-1].request
    body: dict[str, Any] = {
        "mode": "SHARE_ONLY",
        "action_version": 1,
        "recipient_group_ids": [group["id"]],
    }
    if send_at is not None:
        body["send_at"] = send_at.isoformat()
    assert client.post(f"/approval-requests/{review.id}/commit", json=body).status_code == 200
    return review.id


def _share(client: TestClient, review_id: str) -> dict[str, Any]:
    shares = client.get(f"/approval-requests/{review_id}").json()["shares"]
    return shares[0]  # type: ignore[no-any-return]


class TestSendLater:
    def test_goes_out_at_its_time_hours_after_approval(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        send_at = EVENING + 4 * HOUR + 30 * MINUTE  # 17:00 approval, 21:30 send
        _approve(client, send_at=send_at)

        early = run_tick(whatsapp, now=send_at - MINUTE)
        assert early.actions_claimed == 0
        assert whatsapp.delivered_count("test@g.us") == 0

        due = run_tick(whatsapp, now=send_at + 5 * SECOND)
        assert due.sent == 1
        assert whatsapp.delivered_count("test@g.us") == 1

    def test_five_minutes_later(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        _approve(client, send_at=EVENING + 5 * MINUTE)

        assert run_tick(whatsapp, now=EVENING + 4 * MINUTE).sent == 0
        assert run_tick(whatsapp, now=EVENING + 5 * MINUTE + SECOND).sent == 1

    def test_delay_minutes_is_counted_by_the_servers_clock(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        group = client.post(
            "/whatsapp/groups", json={"display_name": "Test", "external_jid": "test@g.us"}
        ).json()
        review = trigger_reviews(now=EVENING)[-1].request

        commit = client.post(
            f"/approval-requests/{review.id}/commit",
            json={
                "mode": "SHARE_ONLY",
                "action_version": 1,
                "recipient_group_ids": [group["id"]],
                "delay_minutes": 5,
            },
        )

        assert commit.status_code == 200
        [share] = client.get(f"/approval-requests/{review.id}").json()["shares"]
        assert dt.datetime.fromisoformat(share["run_at"]) == EVENING + 5 * MINUTE
        assert run_tick(whatsapp, now=EVENING + 4 * MINUTE).sent == 0
        assert run_tick(whatsapp, now=EVENING + 5 * MINUTE + SECOND).sent == 1

    def test_send_at_and_delay_minutes_together_are_refused(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(EVENING)
        review = trigger_reviews(now=EVENING)[-1].request

        response = client.post(
            f"/approval-requests/{review.id}/commit",
            json={
                "mode": "UPDATE_ONLY",
                "action_version": 1,
                "send_at": (EVENING + HOUR).isoformat(),
                "delay_minutes": 5,
            },
        )

        assert response.status_code == 422

    def test_scheduled_for_tomorrow_morning_sends_tomorrow_morning(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        tomorrow_9 = EVENING + 16 * HOUR  # 09:00 IST next day
        _approve(client, send_at=tomorrow_9)

        assert run_tick(whatsapp, now=tomorrow_9 - MINUTE).sent == 0
        assert run_tick(whatsapp, now=tomorrow_9 + 5 * SECOND).sent == 1


class TestWhatsAppDown:
    def test_due_report_waits_without_burning_retries_then_sends_on_reconnect(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        whatsapp.given_connection(ConnectionState.UNAVAILABLE)
        review_id = _approve(client)

        for seconds in (5, 35, 155, 700):
            assert run_tick(whatsapp, now=EVENING + dt.timedelta(seconds=seconds)).sent == 0

        assert whatsapp.send_attempts == 0, "must not even try while WhatsApp is down"
        waiting = _share(client, review_id)
        assert waiting["job"]["state"] == "SCHEDULED"
        assert [r["state"] for r in waiting["recipients"]] == ["QUEUED"]
        assert [r["attempts"] for r in waiting["recipients"]] == [0]

        whatsapp.given_connection(ConnectionState.CONNECTED)
        back = run_tick(whatsapp, now=EVENING + 20 * MINUTE)
        assert back.sent == 1
        assert whatsapp.delivered_count("test@g.us") == 1
        assert _share(client, review_id)["job"]["state"] == "SENT"

    def test_outage_past_the_grace_window_holds_the_report_back_and_says_why(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        whatsapp.given_connection(ConnectionState.UNAVAILABLE)
        review_id = _approve(client)
        run_tick(whatsapp, now=EVENING + 5 * SECOND)

        # Reconnects two hours later -- beyond the 60-minute grace.
        whatsapp.given_connection(ConnectionState.CONNECTED)
        result = run_tick(whatsapp, now=EVENING + 2 * HOUR)

        assert result.sent == 0
        assert whatsapp.delivered_count("test@g.us") == 0
        job = _share(client, review_id)["job"]
        assert job["state"] == "DEFERRED"
        assert job["deferred_reason"] == "WHATSAPP_DISCONNECTED"

    def test_outage_across_midnight_never_sends_yesterdays_report(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        whatsapp.given_connection(ConnectionState.UNAVAILABLE)
        review_id = _approve(client)
        run_tick(whatsapp, now=EVENING + 5 * SECOND)

        whatsapp.given_connection(ConnectionState.CONNECTED)
        next_morning = run_tick(whatsapp, now=EVENING + 16 * HOUR)  # 09:00 next day

        assert next_morning.sent == 0
        assert whatsapp.delivered_count("test@g.us") == 0
        job = _share(client, review_id)["job"]
        assert job["state"] == "DEFERRED"
        assert job["deferred_reason"] == "WHATSAPP_DISCONNECTED"

    def test_still_down_when_it_is_too_late_is_held_back_not_left_waiting(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        whatsapp.given_connection(ConnectionState.UNAVAILABLE)
        review_id = _approve(client)

        run_tick(whatsapp, now=EVENING + 2 * HOUR)  # WhatsApp still down

        job = _share(client, review_id)["job"]
        assert job["state"] == "DEFERRED"
        assert job["deferred_reason"] == "WHATSAPP_DISCONNECTED"


class TestRetryAfterTheDayEnds:
    def test_automatic_retry_does_not_deliver_yesterdays_report(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        whatsapp.given_failure(
            "test@g.us", code="SOCKET", error_class=ErrorClass.TRANSIENT, times=1
        )
        review_id = _approve(client)

        first = run_tick(whatsapp, now=EVENING + 5 * SECOND)
        assert first.sent == 0
        assert _share(client, review_id)["recipients"][0]["state"] == "QUEUED"

        # The retry comes due only the next morning (laptop was off overnight).
        morning = run_tick(whatsapp, now=EVENING + 16 * HOUR)

        assert morning.sent == 0
        assert whatsapp.delivered_count("test@g.us") == 0
        [recipient] = _share(client, review_id)["recipients"]
        assert recipient["state"] == "FAILED"
        assert recipient["error_code"] == "MISSED_DAY"

    def test_a_human_pressing_retry_is_not_held_to_the_day(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        whatsapp.given_failure(
            "test@g.us", code="SOCKET", error_class=ErrorClass.TRANSIENT, times=1
        )
        review_id = _approve(client)
        run_tick(whatsapp, now=EVENING + 5 * SECOND)
        run_tick(whatsapp, now=EVENING + 16 * HOUR)
        [recipient] = _share(client, review_id)["recipients"]
        assert recipient["state"] == "FAILED"

        clock.set_to(EVENING + 16 * HOUR)
        retry = client.post(f"/shares/recipients/{recipient['id']}/retry")

        assert retry.status_code == 200
        assert retry.json()["state"] == "SENT"
