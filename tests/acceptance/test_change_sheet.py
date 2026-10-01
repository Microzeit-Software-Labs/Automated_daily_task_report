"""Changing the Google Sheet from the UI, end to end: the pasted link is checked
before anything is saved, a failed check changes nothing, the worker imports the
new sheet, and switching sheets never mixes two sheets' tasks (the same Sr No.
in two sheets is two different tasks).
"""

from __future__ import annotations

import datetime as dt
import urllib.error
from typing import Any

from starlette.testclient import TestClient

from interlock.adapters.sheets.csv_source import SheetNotReadableError
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.api.main import create_app
from interlock.config import Settings
from interlock.domain.common.clock import FrozenClock
from tests.acceptance.conftest import (
    T0,
    FakeSheetReader,
    _test_settings,
    run_sheet_import,
)

LINK_A = "https://docs.google.com/spreadsheets/d/sheetA/edit#gid=0"
LINK_B = "https://docs.google.com/spreadsheets/d/sheetB/edit#gid=0"
NOW = T0
LATER = T0 + dt.timedelta(minutes=1)

# The same Sr No. (1 and 2) in both sheets, with different tasks.
SHEET_A = (
    "1,01-09-2026,A: renew certificates,Pending,,10-10-2026\n"
    "2,02-09-2026,A: audit logs,Inprogress,,\n"
    "3,03-09-2026,A: patch servers,Closed,,\n"
)
SHEET_B = "1,01-09-2026,B: onboard vendor,Pending,,\n2,02-09-2026,B: update runbook,Inprogress,,\n"


def titles(client: TestClient) -> list[str]:
    return sorted(t["title"] for t in client.get("/tasks").json())


def task_ids(client: TestClient) -> dict[str, str]:
    return {t["title"]: t["id"] for t in client.get("/tasks").json()}


def save(client: TestClient, link: str) -> Any:
    return client.put("/sheet", json={"url": link})


class TestNothingConfigured:
    def test_the_sheet_is_not_connected(self, client: TestClient) -> None:
        body = client.get("/sheet").json()

        assert body["configured"] is False
        assert body["changeable"] is True
        assert body["url"] is None

    def test_ui_config_has_no_sheet_url(self, client: TestClient) -> None:
        assert client.get("/config/ui").json()["sheet_url"] is None


class TestCheckingALinkSavesNothing:
    def test_a_good_sheet_reports_what_it_holds(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SHEET_A)

        body = client.post("/sheet/check", json={"url": LINK_A}).json()

        assert body["ok"] is True
        assert body["task_count"] == 3
        assert body["tab"] == "0"
        assert body["sample_titles"] == [
            "A: renew certificates",
            "A: audit logs",
            "A: patch servers",
        ]
        assert body["will_hide"] == 0
        assert client.get("/sheet").json()["configured"] is False  # nothing was saved

    def test_the_tab_comes_from_the_link(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SHEET_A)

        body = client.post(
            "/sheet/check", json={"url": "https://docs.google.com/spreadsheets/d/sheetA/edit#gid=42"}
        ).json()

        assert body["tab"] == "42"

    def test_something_that_is_not_a_link(self, client: TestClient) -> None:
        body = client.post("/sheet/check", json={"url": "my tasks"}).json()

        assert body["ok"] is False
        assert body["problem"]["code"] == "NOT_A_SHEET_LINK"

    def test_a_private_sheet_says_how_to_share_it(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SheetNotReadableError("sign-in page"))

        body = client.post("/sheet/check", json={"url": LINK_A}).json()

        assert body["problem"]["code"] == "NOT_SHARED"
        assert "Anyone with the link" in body["problem"]["message"]

    def test_a_sheet_that_does_not_exist(self, client: TestClient) -> None:
        body = client.post("/sheet/check", json={"url": LINK_A}).json()

        assert body["problem"]["code"] == "NOT_FOUND"

    def test_a_sheet_with_the_wrong_columns(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", "1,x\n", header="Name,Colour\n")

        body = client.post("/sheet/check", json={"url": LINK_A}).json()

        assert body["problem"]["code"] == "WRONG_COLUMNS"
        assert "Sr No." in body["problem"]["message"]

    def test_a_sheet_with_a_header_but_no_tasks(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", "")

        body = client.post("/sheet/check", json={"url": LINK_A}).json()

        assert body["problem"]["code"] == "EMPTY"

    def test_no_internet(self, client: TestClient, sheet_reader: FakeSheetReader) -> None:
        sheet_reader.serve("sheetA", urllib.error.URLError("no route"))

        body = client.post("/sheet/check", json={"url": LINK_A}).json()

        assert body["problem"]["code"] == "UNREACHABLE"


class TestConnectingASheet:
    def test_saving_asks_the_worker_to_import_and_it_does(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SHEET_A)

        saved = save(client, LINK_A)

        assert saved.status_code == 200
        assert saved.json()["configured"] is True
        assert saved.json()["importing"] is True
        assert titles(client) == []  # nothing is imported until the worker ticks

        assert run_sheet_import(sheet_reader, now=LATER) is True

        status = client.get("/sheet").json()
        assert status["importing"] is False
        assert status["last_task_count"] == 3
        assert status["last_error"] is None
        assert titles(client) == ["A: audit logs", "A: patch servers", "A: renew certificates"]
        assert client.get("/config/ui").json()["sheet_url"] == LINK_A

    def test_the_worker_does_not_read_again_until_the_interval(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SHEET_A)
        save(client, LINK_A)
        run_sheet_import(sheet_reader, now=LATER)
        reads = len(sheet_reader.reads)

        assert run_sheet_import(sheet_reader, now=LATER, interval_due=False) is False
        assert len(sheet_reader.reads) == reads
        assert run_sheet_import(sheet_reader, now=LATER, interval_due=True) is True

    def test_a_sheet_that_cannot_be_read_is_not_saved(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SheetNotReadableError("sign-in page"))

        response = save(client, LINK_A)

        assert response.status_code == 422
        assert response.json()["error"]["details"]["code"] == "NOT_SHARED"
        assert client.get("/sheet").json()["configured"] is False

    def test_the_saved_link_survives_a_restart(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SHEET_A)
        save(client, LINK_A)

        restarted = create_app(
            settings=_test_settings(),
            clock=FrozenClock(T0),
            whatsapp_provider=MockWhatsAppProvider(FrozenClock(T0)),
            sheet_reader=sheet_reader,
        )
        with TestClient(restarted) as again:
            assert again.get("/sheet").json()["url"] == LINK_A

    def test_every_change_is_audited(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SHEET_A)
        sheet_reader.serve("sheetB", SHEET_B)
        save(client, LINK_A)
        save(client, LINK_B)

        from sqlalchemy import text

        from interlock.adapters.persistence.base import make_engine

        engine = make_engine(_test_settings().database_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT after FROM audit_logs "
                        "WHERE action = 'SHEET_LINK_CHANGED' ORDER BY seq"
                    )
                ).all()
        finally:
            engine.dispose()

        assert [r[0]["url"] for r in rows] == [LINK_A, LINK_B]


class TestSwitchingSheets:
    def _connect_a(self, client: TestClient, reader: FakeSheetReader) -> dict[str, str]:
        reader.serve("sheetA", SHEET_A)
        save(client, LINK_A)
        run_sheet_import(reader, now=NOW)
        return task_ids(client)

    def test_the_check_says_how_many_current_tasks_would_be_hidden(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        self._connect_a(client, sheet_reader)
        sheet_reader.serve("sheetB", SHEET_B)

        body = client.post("/sheet/check", json={"url": LINK_B}).json()

        assert body["ok"] is True
        assert body["will_hide"] == 3
        assert body["same_sheet"] is False

    def test_the_same_sheet_hides_nothing(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        self._connect_a(client, sheet_reader)

        body = client.post("/sheet/check", json={"url": LINK_A}).json()

        assert body["same_sheet"] is True
        assert body["will_hide"] == 0

    def test_the_new_sheets_tasks_replace_the_old_ones_in_reports(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        ids_a = self._connect_a(client, sheet_reader)
        sheet_reader.serve("sheetB", SHEET_B)

        save(client, LINK_B)
        run_sheet_import(sheet_reader, now=LATER)

        assert titles(client) == ["B: onboard vendor", "B: update runbook"]
        ids_b = task_ids(client)
        # Sr No. 1 and 2 exist in both sheets, yet B's tasks are new tasks:
        # nothing of A was overwritten or reused.
        assert set(ids_b.values()).isdisjoint(ids_a.values())

    def test_switching_back_restores_the_first_sheets_own_tasks(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        ids_a = self._connect_a(client, sheet_reader)
        sheet_reader.serve("sheetB", SHEET_B)
        save(client, LINK_B)
        run_sheet_import(sheet_reader, now=LATER)

        save(client, LINK_A)
        run_sheet_import(sheet_reader, now=LATER + dt.timedelta(minutes=1))

        assert titles(client) == ["A: audit logs", "A: patch servers", "A: renew certificates"]
        assert task_ids(client) == ids_a  # the very same tasks, not copies

    def test_a_failed_import_of_the_new_sheet_keeps_the_old_tasks_and_says_why(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        self._connect_a(client, sheet_reader)
        sheet_reader.serve("sheetB", SHEET_B)
        save(client, LINK_B)
        # B stops being shared between the check and the worker's tick.
        sheet_reader.serve("sheetB", SheetNotReadableError("sign-in page"))

        run_sheet_import(sheet_reader, now=LATER)

        status = client.get("/sheet").json()
        assert status["last_error_code"] == "NOT_SHARED"
        assert "Anyone with the link" in status["last_error"]
        assert status["importing"] is False
        assert titles(client) == ["A: audit logs", "A: patch servers", "A: renew certificates"]

    def test_a_later_success_clears_the_error(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SHEET_A)
        save(client, LINK_A)
        sheet_reader.serve("sheetA", SheetNotReadableError("sign-in page"))
        run_sheet_import(sheet_reader, now=LATER)
        assert client.get("/sheet").json()["last_error_code"] == "NOT_SHARED"

        sheet_reader.serve("sheetA", SHEET_A)
        run_sheet_import(sheet_reader, now=LATER + dt.timedelta(minutes=2), interval_due=True)

        status = client.get("/sheet").json()
        assert status["last_error"] is None
        assert status["last_task_count"] == 3


class TestWhileTheTwoWaySyncOwnsTheSheet:
    def test_the_link_cannot_be_changed(self, client: TestClient) -> None:
        client.app.state.sheet_reader = None  # type: ignore[attr-defined]

        assert client.get("/sheet").json()["changeable"] is False
        response = save(client, LINK_A)
        assert response.status_code == 422
        assert response.json()["error"]["details"]["code"] == "TWO_WAY_SYNC"


class TestSeededFromTheEnvironment:
    def _app_with_env_link(self, reader: FakeSheetReader) -> TestClient:
        settings: Settings = _test_settings().model_copy(update={"sheet_import_url": LINK_A})
        app = create_app(
            settings=settings,
            clock=FrozenClock(T0),
            whatsapp_provider=MockWhatsAppProvider(FrozenClock(T0)),
            sheet_reader=reader,
        )
        return TestClient(app)

    def test_an_existing_env_link_becomes_the_saved_setting_once(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        sheet_reader.serve("sheetA", SHEET_A)
        sheet_reader.serve("sheetB", SHEET_B)

        with self._app_with_env_link(sheet_reader) as env_client:
            assert env_client.get("/sheet").json()["url"] == LINK_A
            # From now on the saved setting wins: changing it in the browser sticks
            # even though .env still says A.
            assert env_client.put("/sheet", json={"url": LINK_B}).status_code == 200

        with self._app_with_env_link(sheet_reader) as env_client:
            assert env_client.get("/sheet").json()["url"] == LINK_B

    def test_a_first_import_that_fails_is_still_recorded(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        """The worker seeds the setting from .env and then fails to read the sheet
        (say the machine is offline). Rolling the failed import back must not take
        the seed with it, or the failure has no row to be written on and the page
        never learns why."""
        settings = _test_settings().model_copy(update={"sheet_import_url": LINK_A})
        sheet_reader.serve("sheetA", urllib.error.URLError("no route"))

        assert run_sheet_import(sheet_reader, now=NOW, interval_due=True, settings=settings)

        status = client.get("/sheet").json()
        assert status["configured"] is True
        assert status["last_error_code"] == "UNREACHABLE"

    def test_tasks_imported_before_scopes_existed_keep_their_identity(
        self, client: TestClient, sheet_reader: FakeSheetReader
    ) -> None:
        """A database from before this feature has plain ``sr:<n>`` refs. The
        first time the env link is recorded they are stamped with its scope, so
        the next import updates them instead of duplicating them."""
        from sqlalchemy import text

        from interlock.adapters.persistence.base import make_engine

        sheet_reader.serve("sheetA", SHEET_A)
        engine = make_engine(_test_settings().database_url)
        try:
            with TestClient(
                create_app(
                    settings=_test_settings(),
                    clock=FrozenClock(T0),
                    whatsapp_provider=MockWhatsAppProvider(FrozenClock(T0)),
                    sheet_reader=sheet_reader,
                )
            ) as plain:
                save(plain, LINK_A)
                run_sheet_import(sheet_reader, now=NOW)
                ids = task_ids(plain)
            with engine.begin() as conn:  # rewind to the old format, as if never scoped
                conn.execute(text("DELETE FROM sheet_source"))
                conn.execute(
                    text(
                        "UPDATE tasks SET external_row_ref = substring(external_row_ref from "
                        "'sr:.*$') WHERE source = 'SHEET_IMPORT'"
                    )
                )
        finally:
            engine.dispose()

        with self._app_with_env_link(sheet_reader) as env_client:
            assert env_client.get("/sheet").json()["url"] == LINK_A  # seeds and stamps
            run_sheet_import(sheet_reader, now=LATER)

            assert task_ids(env_client) == ids  # updated in place, none duplicated
