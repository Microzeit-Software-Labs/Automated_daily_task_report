"""The 09:00 / 17:00 popup, end to end through the real API.

The popup is derived from the open review in Postgres, so these tests check
the promises that matter for a notification service rather than a timer:
it appears once, snoozing silences it and brings it back on time, it
survives a restart, and acting on it sends exactly once.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from starlette.testclient import TestClient

from interlock.adapters.sheets.fake import FakeSpreadsheetProvider
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.api.main import create_app
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import (
    EVENING,
    MORNING,
    _test_settings,
    run_tick,
    trigger_reviews,
)

MINUTE = dt.timedelta(minutes=1)


def _prompt(client: TestClient) -> dict[str, Any] | None:
    response = client.get("/notifications/prompt")
    assert response.status_code == 200
    return response.json()["prompt"]  # type: ignore[no-any-return]


def _group(client: TestClient) -> str:
    return client.post(  # type: ignore[no-any-return]
        "/whatsapp/groups", json={"display_name": "Test", "external_jid": "test@g.us"}
    ).json()["id"]


class TestAppears:
    def test_no_prompt_before_a_review_opens(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(MORNING - MINUTE)
        assert _prompt(client) is None

    def test_prompts_once_the_review_opens(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(MORNING)
        review = trigger_reviews(now=MORNING)[0].request

        prompt = _prompt(client)

        assert prompt is not None
        assert prompt["id"] == review.id
        assert prompt["kind"] == "MORNING"
        assert prompt["state"] == "REVIEW_PENDING"

    def test_asking_twice_shows_the_same_single_prompt(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(MORNING)
        trigger_reviews(now=MORNING)
        trigger_reviews(now=MORNING + MINUTE)  # the worker ticks again: no duplicate review

        first, second = _prompt(client), _prompt(client)

        assert first is not None and first == second
        assert len(client.get("/approval-requests").json()) == 1

    def test_evening_replaces_an_ignored_morning(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(EVENING)
        results = trigger_reviews(now=EVENING)  # morning and evening both exist by 17:00

        prompt = _prompt(client)

        assert prompt is not None
        assert prompt["id"] == results[-1].request.id
        assert prompt["kind"] == "EVENING"


class TestSnooze:
    def test_silenced_then_back_after_thirty_minutes(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(MORNING)
        review = trigger_reviews(now=MORNING)[0].request

        snoozed = client.post(f"/approval-requests/{review.id}/snooze", json={"minutes": 30})
        assert snoozed.status_code == 200
        assert dt.datetime.fromisoformat(snoozed.json()["snoozed_until"]) == MORNING + 30 * MINUTE

        clock.set_to(MORNING + 29 * MINUTE)
        assert _prompt(client) is None
        clock.set_to(MORNING + 30 * MINUTE)
        again = _prompt(client)
        assert again is not None and again["id"] == review.id

    def test_defaults_to_thirty_minutes(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(MORNING)
        review = trigger_reviews(now=MORNING)[0].request

        snoozed = client.post(f"/approval-requests/{review.id}/snooze", json={}).json()

        assert dt.datetime.fromisoformat(snoozed["snoozed_until"]) == MORNING + 30 * MINUTE

    def test_snoozing_again_moves_the_time(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(MORNING)
        review = trigger_reviews(now=MORNING)[0].request
        client.post(f"/approval-requests/{review.id}/snooze", json={"minutes": 30})

        clock.set_to(MORNING + 30 * MINUTE)
        client.post(f"/approval-requests/{review.id}/snooze", json={"minutes": 30})

        clock.set_to(MORNING + 45 * MINUTE)
        assert _prompt(client) is None
        clock.set_to(MORNING + 60 * MINUTE)
        assert _prompt(client) is not None

    def test_survives_a_full_restart(self, client: TestClient, clock: FrozenClock) -> None:
        """Only Postgres carries state across: a brand new app sees the snooze."""
        clock.set_to(MORNING)
        review = trigger_reviews(now=MORNING)[0].request
        client.post(f"/approval-requests/{review.id}/snooze", json={"minutes": 30})

        restarted_clock = FrozenClock(MORNING + 10 * MINUTE)
        restarted_app = create_app(
            settings=_test_settings(),
            clock=restarted_clock,
            whatsapp_provider=MockWhatsAppProvider(restarted_clock),
            sheets_provider=FakeSpreadsheetProvider(),
        )
        with TestClient(restarted_app) as restarted:
            assert _prompt(restarted) is None
            restarted_clock.set_to(MORNING + 31 * MINUTE)
            back = _prompt(restarted)
            assert back is not None and back["id"] == review.id

    def test_rejects_nonsense_minutes(self, client: TestClient, clock: FrozenClock) -> None:
        clock.set_to(MORNING)
        review = trigger_reviews(now=MORNING)[0].request

        for minutes in (0, -5, 241):
            response = client.post(
                f"/approval-requests/{review.id}/snooze", json={"minutes": minutes}
            )
            assert response.status_code == 422

    def test_a_handled_review_cannot_be_snoozed(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(MORNING)
        review = trigger_reviews(now=MORNING)[0].request
        client.post(
            f"/approval-requests/{review.id}/commit",
            json={"mode": "UPDATE_ONLY", "action_version": 1},
        )

        response = client.post(f"/approval-requests/{review.id}/snooze", json={"minutes": 30})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"

    def test_unknown_review_is_404(self, client: TestClient) -> None:
        assert client.post("/approval-requests/nope/snooze", json={}).status_code == 404


class TestActingOnIt:
    def test_send_now_sends_once_and_clears_the_prompt(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        group_id = _group(client)
        review = trigger_reviews(now=EVENING)[-1].request
        preview = client.post(f"/approval-requests/{review.id}/preview").json()
        body = {
            "mode": "SHARE_ONLY",
            "action_version": 1,
            "recipient_group_ids": [group_id],
            "expected_content_hash": preview["content_hash"],
        }

        first = client.post(f"/approval-requests/{review.id}/commit", json=body)
        double_click = client.post(f"/approval-requests/{review.id}/commit", json=body)
        run_tick(whatsapp, now=EVENING + 5 * dt.timedelta(seconds=1))

        assert first.status_code == double_click.status_code == 200
        assert double_click.json()["already_committed"] is True
        assert whatsapp.delivered_count("test@g.us") == 1
        assert _prompt(client) is None

    def test_send_after_five_minutes_waits_then_sends_and_clears_the_prompt(
        self, client: TestClient, clock: FrozenClock, whatsapp: MockWhatsAppProvider
    ) -> None:
        clock.set_to(EVENING)
        group_id = _group(client)
        review = trigger_reviews(now=EVENING)[-1].request

        client.post(
            f"/approval-requests/{review.id}/commit",
            json={
                "mode": "SHARE_ONLY",
                "action_version": 1,
                "recipient_group_ids": [group_id],
                "send_at": (EVENING + 5 * MINUTE).isoformat(),
            },
        )

        assert _prompt(client) is None, "approved: nothing left to ask about"
        assert run_tick(whatsapp, now=EVENING + 4 * MINUTE).sent == 0
        assert run_tick(whatsapp, now=EVENING + 5 * MINUTE + dt.timedelta(seconds=1)).sent == 1

    def test_skip_closes_the_review_and_clears_the_prompt(
        self, client: TestClient, clock: FrozenClock
    ) -> None:
        clock.set_to(EVENING)
        review = trigger_reviews(now=EVENING)[-1].request

        client.post(
            f"/approval-requests/{review.id}/commit",
            json={"mode": "UPDATE_ONLY", "action_version": 1},
        )

        assert _prompt(client) is None
