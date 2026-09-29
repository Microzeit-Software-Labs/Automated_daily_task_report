"""The HTTP surface for parked sync conflicts: list and resolve.

Only proves the new DI wiring (``app.state.sheets``,
``deps.get_sheet_sync_service``, the ``sync`` router) actually works end to
end -- the reconciliation behaviour itself is covered thoroughly in
tests/integration/test_sheet_sync_service.py and
tests/integration/test_sync_repository.py, at the service/repository layer
this router is a thin pass-through to.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from interlock.adapters.persistence.base import make_engine
from interlock.adapters.persistence.sync_repository import SyncRepository
from tests.acceptance.conftest import T0, _test_settings


def seed_conflict(task_id: str, *, external_value: dict[str, Any] | None) -> str:
    """Parks a conflict directly against the test database, bypassing
    ``SheetSyncService.tick()`` -- this file is about the HTTP surface for
    resolving a conflict, not about how one gets created."""
    settings = _test_settings()
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            session = sessionmaker(bind=conn, future=True)()
            conflict = SyncRepository(session).record_conflict(
                task_id,
                detected_at=T0,
                db_value={"title": "DB value"},
                external_value=external_value,
            )
            session.commit()
            return conflict.id
    finally:
        engine.dispose()


def create_task(client: TestClient) -> dict[str, Any]:
    response = client.post("/tasks", json={"title": "Deploy production server"})
    assert response.status_code == 201
    return response.json()  # type: ignore[no-any-return]


class TestListConflicts:
    def test_empty_when_nothing_is_parked(self, client: TestClient) -> None:
        response = client.get("/sync/conflicts")
        assert response.status_code == 200
        assert response.json() == []

    def test_lists_an_open_conflict(self, client: TestClient) -> None:
        task = create_task(client)
        conflict_id = seed_conflict(task["id"], external_value={"title": "Sheet value"})

        response = client.get("/sync/conflicts")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["id"] == conflict_id
        assert body[0]["task_id"] == task["id"]
        assert body[0]["is_open"] is True

    def test_open_only_excludes_resolved_conflicts_by_default(self, client: TestClient) -> None:
        task = create_task(client)
        conflict_id = seed_conflict(task["id"], external_value={"title": "Sheet value"})
        resolved = client.post(
            f"/sync/conflicts/{conflict_id}/resolve", json={"resolution": "kept_db"}
        )
        assert resolved.status_code == 200

        response = client.get("/sync/conflicts")
        assert response.json() == []

    def test_open_only_false_includes_resolved_conflicts(self, client: TestClient) -> None:
        task = create_task(client)
        conflict_id = seed_conflict(task["id"], external_value={"title": "Sheet value"})
        client.post(f"/sync/conflicts/{conflict_id}/resolve", json={"resolution": "kept_db"})

        response = client.get("/sync/conflicts", params={"open_only": False})

        ids = [c["id"] for c in response.json()]
        assert conflict_id in ids


class TestResolveConflict:
    def test_kept_db_returns_200_and_closes_it(self, client: TestClient) -> None:
        task = create_task(client)
        conflict_id = seed_conflict(task["id"], external_value={"title": "Sheet value"})

        response = client.post(
            f"/sync/conflicts/{conflict_id}/resolve", json={"resolution": "kept_db"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["is_open"] is False
        assert body["resolution"] == "kept_db"

    def test_resolving_twice_returns_409(self, client: TestClient) -> None:
        task = create_task(client)
        conflict_id = seed_conflict(task["id"], external_value={"title": "Sheet value"})
        client.post(f"/sync/conflicts/{conflict_id}/resolve", json={"resolution": "kept_db"})

        response = client.post(
            f"/sync/conflicts/{conflict_id}/resolve", json={"resolution": "kept_sheet"}
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SYNC_CONFLICT_ALREADY_RESOLVED"

    def test_unknown_conflict_id_returns_404(self, client: TestClient) -> None:
        response = client.post(
            "/sync/conflicts/not-a-real-id/resolve", json={"resolution": "kept_db"}
        )
        assert response.status_code == 404

    def test_a_bogus_resolution_value_returns_422(self, client: TestClient) -> None:
        task = create_task(client)
        conflict_id = seed_conflict(task["id"], external_value={"title": "Sheet value"})

        response = client.post(
            f"/sync/conflicts/{conflict_id}/resolve", json={"resolution": "not_a_real_value"}
        )

        assert response.status_code == 422
