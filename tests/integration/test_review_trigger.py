"""The 09:00 / 17:00 trigger against a real database."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from interlock.adapters.persistence.approval_repository import ApprovalRepository
from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.domain.approvals.request import ApprovalKind
from interlock.services.review_service import ReviewService
from interlock.services.review_trigger import maybe_create_daily_reviews

TZ = ZoneInfo("Asia/Kolkata")
WORKING_DAYS = frozenset({0, 1, 2, 3, 4})  # Mon-Fri
MORNING = dt.time(9, 0)
EVENING = dt.time(17, 0)

# Tuesday 15 September 2026.
BEFORE_MORNING = dt.datetime(2026, 9, 15, 3, 0, tzinfo=dt.UTC)  # 08:30 IST
AFTER_MORNING = dt.datetime(2026, 9, 15, 4, 0, tzinfo=dt.UTC)  # 09:30 IST
AFTER_EVENING = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)  # 17:30 IST
SATURDAY = dt.datetime(2026, 9, 19, 4, 0, tzinfo=dt.UTC)  # 09:30 IST, Sat 19 Sep


def make_service(session: Session) -> ReviewService:
    return ReviewService(
        ApprovalRepository(session), PostgresTaskRepository(session), PostgresAuditSink(session), TZ
    )


def trigger(service: ReviewService, now: dt.datetime) -> list:
    return maybe_create_daily_reviews(
        service,
        now=now,
        tz=TZ,
        working_days=WORKING_DAYS,
        morning_alert_time=MORNING,
        evening_alert_time=EVENING,
    )


class TestBeforeAlertTime:
    def test_nothing_is_created_before_nine_am(self, db_session: Session) -> None:
        service = make_service(db_session)
        results = trigger(service, BEFORE_MORNING)
        assert results == []


class TestAfterAlertTime:
    def test_morning_review_is_created_after_nine_am(self, db_session: Session) -> None:
        service = make_service(db_session)
        results = trigger(service, AFTER_MORNING)
        assert len(results) == 1
        assert results[0].kind is ApprovalKind.MORNING
        assert results[0].newly_created

    def test_evening_trigger_also_creates_the_morning_review_if_missed(
        self, db_session: Session
    ) -> None:
        """Both alert times have passed by 17:30 -- both reviews should
        exist, not just the evening one."""
        service = make_service(db_session)
        results = trigger(service, AFTER_EVENING)
        kinds = {r.kind for r in results}
        assert kinds == {ApprovalKind.MORNING, ApprovalKind.EVENING}


class TestIdempotency:
    def test_calling_repeatedly_creates_exactly_one_review(self, db_session: Session) -> None:
        """A worker ticking every 30 seconds must not create a new morning
        review on every single tick after 09:00."""
        service = make_service(db_session)
        first = trigger(service, AFTER_MORNING)
        second = trigger(service, AFTER_MORNING + dt.timedelta(minutes=1))
        third = trigger(service, AFTER_MORNING + dt.timedelta(minutes=2))

        assert first[0].newly_created
        assert not second[0].newly_created
        assert not third[0].newly_created
        assert first[0].request.id == second[0].request.id == third[0].request.id


class TestWorkingDays:
    def test_nothing_is_created_on_a_non_working_day(self, db_session: Session) -> None:
        service = make_service(db_session)
        results = trigger(service, SATURDAY)
        assert results == []
