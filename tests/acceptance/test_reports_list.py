"""What the Reports page needs from ``GET /approval-requests``: each report with
how its latest send went (so the page can say "Sent to 3 groups" instead of
"Approved"), the ``since`` filter behind its Today / 7 days / 30 days switch, and
the schedule the Settings page shows.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import EVENING, run_tick, trigger_reviews

MINUTE = dt.timedelta(minutes=1)
SECOND = dt.timedelta(seconds=1)


def _group(client: TestClient, name: str, jid: str) -> str:
    response = client.post("/whatsapp/groups", json={"display_name": name, "external_jid": jid})
    assert response.status_code == 201
    return str(response.json()["id"])


def _listed(client: TestClient, review_id: str) -> dict[str, Any]:
    """This review's row on the Reports list."""
    rows: list[dict[str, Any]] = client.get("/approval-requests").json()
    return next(r for r in rows if r["id"] == review_id)


def _commit(client: TestClient, review_id: str, **body: Any) -> None:
    payload = {"mode": "SHARE_ONLY", "action_version": 1, **body}
    response = client.post(f"/approval-requests/{review_id}/commit", json=payload)
    assert response.status_code == 200, response.text


class TestDeliveryOnTheList:
    def test_a_report_never_shared_has_no_delivery(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(EVENING)
        review = trigger_reviews(now=EVENING)[-1].request
        client.post(
            f"/approval-requests/{review.id}/commit",
            json={"mode": "UPDATE_ONLY", "action_version": 1},
        )

        item = _listed(client, review.id)

        assert item["state"] == "CLOSED_NO_SHARE"
        assert item["delivery"] is None

    def test_a_scheduled_report_says_when_and_to_whom(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(EVENING)
        ops = _group(client, "Ops", "ops@g.us")
        dev = _group(client, "Dev", "dev@g.us")
        review = trigger_reviews(now=EVENING)[-1].request
        send_at = EVENING + 90 * MINUTE
        _commit(client, review.id, recipient_group_ids=[ops, dev], send_at=send_at.isoformat())

        delivery = _listed(client, review.id)[
            "delivery"
        ]

        assert delivery["job_state"] == "SCHEDULED"
        assert dt.datetime.fromisoformat(delivery["run_at"]) == send_at
        assert delivery["sent_at"] is None
        assert (delivery["total"], delivery["sent"], delivery["failed"]) == (2, 0, 0)
        assert delivery["pending"] == 2
        assert delivery["group_names"] == ["Dev", "Ops"]  # sorted, so the row is stable

    def test_a_delivered_report_counts_every_group(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        ops = _group(client, "Ops", "ops@g.us")
        dev = _group(client, "Dev", "dev@g.us")
        review = trigger_reviews(now=EVENING)[-1].request
        _commit(client, review.id, recipient_group_ids=[ops, dev])

        run_tick(whatsapp, now=EVENING + 5 * SECOND)

        delivery = _listed(client, review.id)[
            "delivery"
        ]
        assert delivery["job_state"] == "SENT"
        assert (delivery["total"], delivery["sent"], delivery["failed"], delivery["pending"]) == (
            2,
            2,
            0,
            0,
        )
        assert delivery["sent_at"] is not None

    def test_a_partly_failed_report_says_which_count_failed(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        ops = _group(client, "Ops", "ops@g.us")
        dev = _group(client, "Dev", "dev@g.us")
        whatsapp.given_failure("dev@g.us")  # permanent
        review = trigger_reviews(now=EVENING)[-1].request
        _commit(client, review.id, recipient_group_ids=[ops, dev])

        run_tick(whatsapp, now=EVENING + 5 * SECOND)

        delivery = _listed(client, review.id)[
            "delivery"
        ]
        assert delivery["job_state"] == "PARTIALLY_SENT"
        assert (delivery["total"], delivery["sent"], delivery["failed"]) == (2, 1, 1)

    def test_the_latest_share_is_the_one_summarised(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        ops = _group(client, "Ops", "ops@g.us")
        dev = _group(client, "Dev", "dev@g.us")
        review = trigger_reviews(now=EVENING)[-1].request
        _commit(client, review.id, recipient_group_ids=[ops])
        run_tick(whatsapp, now=EVENING + 5 * SECOND)
        _commit(client, review.id, action_version=2, recipient_group_ids=[ops, dev])

        delivery = _listed(client, review.id)[
            "delivery"
        ]

        assert delivery["total"] == 2
        assert delivery["group_names"] == ["Dev", "Ops"]


class TestSinceFilter:
    def test_since_keeps_that_day_and_later(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(EVENING)
        for days in range(4):
            trigger_reviews(now=EVENING + dt.timedelta(days=days))

        since = client.get("/approval-requests", params={"since": "2026-09-16", "limit": 50}).json()

        assert {r["local_date"] for r in since} == {"2026-09-16", "2026-09-17", "2026-09-18"}

    def test_since_and_local_date_combine(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(EVENING)
        trigger_reviews(now=EVENING)

        assert (
            client.get(
                "/approval-requests", params={"since": "2026-09-16", "local_date": "2026-09-15"}
            ).json()
            == []
        )

    def test_a_bad_date_is_refused(self, client: TestClient) -> None:
        assert client.get("/approval-requests", params={"since": "soon"}).status_code == 422


class TestUiConfigSchedule:
    def test_reports_the_working_days(self, client: TestClient) -> None:
        config = client.get("/config/ui").json()

        assert config["working_days"] == [0, 1, 2, 3, 4]
