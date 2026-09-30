"""The endpoints the review UI needs beyond the nine scenarios: finding
reviews, starting a manual one, the preview/commit drift guard, the delivery
panel, and serving the built app.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient

from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.api.routers.ui import mount_web_app
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import EVENING, MORNING, run_tick, trigger_reviews


def _group(client: TestClient, name: str = "Test Group", jid: str = "test@g.us") -> str:
    response = client.post("/whatsapp/groups", json={"display_name": name, "external_jid": jid})
    assert response.status_code == 201
    return str(response.json()["id"])


class TestListReviews:
    def test_newest_first(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(EVENING)
        trigger_reviews(now=EVENING)

        reviews = client.get("/approval-requests").json()

        assert [r["kind"] for r in reviews] == ["EVENING", "MORNING"]

    def test_filters_by_local_date(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(EVENING)
        trigger_reviews(now=EVENING)
        trigger_reviews(now=EVENING + dt.timedelta(days=1))

        today = client.get("/approval-requests", params={"local_date": "2026-09-15"}).json()

        assert {r["local_date"] for r in today} == {"2026-09-15"}
        assert len(today) == 2

    def test_limit(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(EVENING)
        trigger_reviews(now=EVENING)

        assert len(client.get("/approval-requests", params={"limit": 1}).json()) == 1
        assert client.get("/approval-requests", params={"limit": 0}).status_code == 422


class TestManualReview:
    def test_creates_a_pending_manual_review_each_time(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(MORNING + dt.timedelta(hours=3))

        first = client.post("/approval-requests")
        second = client.post("/approval-requests")

        assert first.status_code == second.status_code == 201
        assert first.json()["kind"] == "MANUAL"
        assert first.json()["state"] == "REVIEW_PENDING"
        assert first.json()["id"] != second.json()["id"]

    def test_manual_review_sends_nothing_until_committed(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        _group(client)
        client.post("/approval-requests")

        run_tick(whatsapp, now=EVENING + dt.timedelta(minutes=1))

        assert whatsapp.delivered_count("test@g.us") == 0


class TestPreviewCommitDrift:
    def test_matching_hash_commits(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(EVENING)
        client.post("/tasks", json={"title": "Ship it"})
        group_id = _group(client)
        review = trigger_reviews(now=EVENING)[-1].request

        preview = client.post(f"/approval-requests/{review.id}/preview").json()
        assert "Ship it" in preview["rendered_body"]

        commit = client.post(
            f"/approval-requests/{review.id}/commit",
            json={
                "mode": "SHARE_ONLY",
                "action_version": 1,
                "recipient_group_ids": [group_id],
                "expected_content_hash": preview["content_hash"],
            },
        )

        assert commit.status_code == 200
        assert commit.json()["job"]["state"] == "SCHEDULED"

    def test_data_changed_after_preview_is_refused_and_nothing_is_approved(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(EVENING)
        task = client.post("/tasks", json={"title": "Ship it"}).json()
        group_id = _group(client)
        review = trigger_reviews(now=EVENING)[-1].request
        stale = client.post(f"/approval-requests/{review.id}/preview").json()

        # The sheet import (or anything else) changes a task after the preview.
        client.patch(
            f"/tasks/{task['id']}",
            json={"status": "COMPLETED"},
            headers={"If-Match": str(task["version"])},
        )

        refused = client.post(
            f"/approval-requests/{review.id}/commit",
            json={
                "mode": "SHARE_ONLY",
                "action_version": 1,
                "recipient_group_ids": [group_id],
                "expected_content_hash": stale["content_hash"],
            },
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "DATASET_VERSION_CONFLICT"

        detail = client.get(f"/approval-requests/{review.id}").json()
        assert detail["request"]["state"] == "REVIEW_PENDING"
        assert detail["shares"] == []

        # Re-preview, and the same action_version now goes through.
        fresh = client.post(f"/approval-requests/{review.id}/preview").json()
        assert fresh["content_hash"] != stale["content_hash"]
        retried = client.post(
            f"/approval-requests/{review.id}/commit",
            json={
                "mode": "SHARE_ONLY",
                "action_version": 1,
                "recipient_group_ids": [group_id],
                "expected_content_hash": fresh["content_hash"],
            },
        )
        assert retried.status_code == 200

    def test_hash_ignored_for_update_only(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(EVENING)
        review = trigger_reviews(now=EVENING)[-1].request

        closed = client.post(
            f"/approval-requests/{review.id}/commit",
            json={"mode": "UPDATE_ONLY", "action_version": 1, "expected_content_hash": "x"},
        )

        assert closed.status_code == 200

    def test_preview_of_unknown_review_is_404(self, client: TestClient) -> None:
        assert client.post("/approval-requests/nope/preview").status_code == 404


class TestDeliveryPanel:
    def test_detail_shows_job_recipients_and_frozen_body(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        client.post("/tasks", json={"title": "Ship it"})
        group_id = _group(client)
        review = trigger_reviews(now=EVENING)[-1].request
        commit = client.post(
            f"/approval-requests/{review.id}/commit",
            json={"mode": "SHARE_ONLY", "action_version": 1, "recipient_group_ids": [group_id]},
        ).json()

        run_tick(whatsapp, now=EVENING + dt.timedelta(seconds=5))
        detail = client.get(f"/approval-requests/{review.id}").json()

        assert detail["request"]["state"] == "APPROVED"
        [share] = detail["shares"]
        assert share["job"]["state"] == "SENT"
        assert share["rendered_body"] == commit["preview"]
        assert dt.datetime.fromisoformat(share["run_at"]) == EVENING
        [recipient] = share["recipients"]
        assert recipient["whatsapp_group_id"] == group_id
        assert recipient["state"] == "SENT"

    def test_detail_of_unshared_review_has_no_shares(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(EVENING)
        review = trigger_reviews(now=EVENING)[-1].request

        assert client.get(f"/approval-requests/{review.id}").json()["shares"] == []


class TestUi:
    def test_config(self, client: TestClient) -> None:
        config = client.get("/config/ui").json()

        assert config["timezone"] == "Asia/Kolkata"
        assert config["morning_alert_time"] == "09:00:00"
        assert set(config) >= {"sheet_url", "user_name", "allow_custom_send_time"}

    def test_serves_built_app_and_redirects_root(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("<!doctype html><title>Interlock</title>")
        app = FastAPI()
        mount_web_app(app, tmp_path)

        with TestClient(app) as c:
            assert c.get("/", follow_redirects=False).headers["location"] == "/app/"
            assert "Interlock" in c.get("/app/").text

    def test_root_falls_back_to_docs_when_not_built(self, tmp_path: Path) -> None:
        app = FastAPI()
        mount_web_app(app, tmp_path)

        with TestClient(app) as c:
            assert c.get("/", follow_redirects=False).headers["location"] == "/docs"
