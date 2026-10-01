"""The installer's Google Sheet step against a real database: it uses the same
check-then-save service as the Settings page, so the terminal and the browser
agree about what is connected."""

from __future__ import annotations

import datetime as dt
import urllib.error
from email.message import Message
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.sheet_source_repository import SheetSourceRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.domain.common.actor import Actor, ActorKind
from interlock.domain.sync.sheet_source import SheetLink
from interlock.services.sheet_source_service import SheetSourceService
from interlock.setup_cli import connect_sheet

TZ = ZoneInfo("Asia/Kolkata")
NOW = dt.datetime(2026, 10, 1, 7, 0, tzinfo=dt.UTC)
HEADER = "Sr No.,Date,Task,Status ,Note,Deadline \n"
GOOD = HEADER + "1,01-09-2026,Renew certificates,Pending,,\n2,02-09-2026,Audit logs,Inprogress,,\n"
LINK_A = "https://docs.google.com/spreadsheets/d/sheetA/edit#gid=0"
LINK_B = "https://docs.google.com/spreadsheets/d/sheetB/edit#gid=0"
INSTALLER = Actor(kind=ActorKind.USER, id="installer", label="installer")


class Talk:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.said: list[str] = []

    def ask(self, prompt: str) -> str:
        if not self.answers:
            raise AssertionError(f"unexpected question: {prompt}")
        return self.answers.pop(0)

    def say(self, message: str) -> None:
        self.said.append(message)

    @property
    def text(self) -> str:
        return "\n".join(self.said)


def reader(link: SheetLink) -> str:
    if link.spreadsheet_id in ("sheetA", "sheetB"):
        return GOOD
    if link.spreadsheet_id == "private":
        raise urllib.error.HTTPError(link.export_url, 403, "Forbidden", Message(), None)
    raise urllib.error.HTTPError(link.export_url, 404, "Not Found", Message(), None)


def run(session: Session, talk: Talk, **kwargs: object) -> tuple[bool, SheetSourceService]:
    service = SheetSourceService(
        repo=SheetSourceRepository(session),
        task_repo=PostgresTaskRepository(session),
        audit=PostgresAuditSink(session),
        tz=TZ,
        reader=reader,
    )
    done = connect_sheet(
        service=service,
        commit=session.flush,
        ask=talk.ask,
        say=talk.say,
        now=lambda: NOW,
        actor=INSTALLER,
        **kwargs,  # type: ignore[arg-type]
    )
    return done, service


def saved_url(service: SheetSourceService) -> str | None:
    state = service.current(now=NOW)
    return state.url if state else None


class TestConnectSheet:
    def test_a_link_given_up_front_is_checked_and_saved(self, db_session: Session) -> None:
        talk = Talk()

        done, service = run(db_session, talk, url=LINK_A)

        assert done is True
        assert saved_url(service) == LINK_A
        assert "Google Sheet connected successfully - 2 tasks found" in talk.text

    def test_it_asks_for_the_link_when_none_is_given(self, db_session: Session) -> None:
        done, service = run(db_session, Talk(LINK_A))

        assert done is True
        assert saved_url(service) == LINK_A

    def test_a_sheet_that_cannot_be_used_is_explained_and_asked_for_again(
        self, db_session: Session
    ) -> None:
        talk = Talk("https://docs.google.com/spreadsheets/d/private/edit", LINK_A)

        done, service = run(db_session, talk)

        assert done is True
        assert "Anyone with the link" in talk.text  # told how to share it
        assert saved_url(service) == LINK_A

    def test_pressing_enter_skips_and_saves_nothing(self, db_session: Session) -> None:
        talk = Talk("")

        done, service = run(db_session, talk)

        assert done is False
        assert saved_url(service) is None
        assert "connect a sheet any time in Settings" in talk.text

    def test_it_gives_up_after_three_bad_links(self, db_session: Session) -> None:
        talk = Talk("my tasks", "still not a link", "nope")

        done, service = run(db_session, talk)

        assert done is False
        assert saved_url(service) is None
        assert "Giving up" in talk.text

    def test_an_already_connected_sheet_is_kept_without_asking(self, db_session: Session) -> None:
        run(db_session, Talk(), url=LINK_A)

        done, service = run(db_session, Talk(), keep_existing=True)

        assert done is True
        assert saved_url(service) == LINK_A

    def test_an_already_connected_sheet_can_be_changed_when_asked(
        self, db_session: Session
    ) -> None:
        run(db_session, Talk(), url=LINK_A)
        talk = Talk("n", LINK_B)

        done, service = run(db_session, talk)

        assert done is True
        assert saved_url(service) == LINK_B

    def test_keeping_is_the_default_answer(self, db_session: Session) -> None:
        run(db_session, Talk(), url=LINK_A)

        done, service = run(db_session, Talk(""))

        assert done is True
        assert saved_url(service) == LINK_A

    @pytest.mark.parametrize("answer", ["n", "N", "no", "NO"])
    def test_only_no_means_change(self, db_session: Session, answer: str) -> None:
        run(db_session, Talk(), url=LINK_A)

        done, service = run(db_session, Talk(answer, LINK_B))

        assert saved_url(service) == LINK_B
        assert done is True
