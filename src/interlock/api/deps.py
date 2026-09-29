"""Dependency providers -- the composition root's per-request wiring.

Every mutating repository and service is constructed here, bound to a single
per-request ``Session`` (``get_session``), so a whole request's work -- task
edits, audit entries, share job creation -- lands in one transaction that
commits or rolls back as a unit.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session, sessionmaker

from interlock.adapters.persistence.approval_repository import ApprovalRepository
from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.models import UserRow
from interlock.adapters.persistence.scheduler_repository import SchedulerRepository
from interlock.adapters.persistence.sequences import PostgresSequences
from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.adapters.persistence.sync_repository import SyncRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.adapters.persistence.whatsapp_group_repository import WhatsAppGroupRepository
from interlock.config import Settings
from interlock.domain.common.actor import Actor, ActorKind
from interlock.domain.common.clock import Clock
from interlock.domain.ports.repositories import AuditSink, TaskRepository
from interlock.domain.ports.sheets import SpreadsheetProvider
from interlock.domain.ports.whatsapp import WhatsAppProvider
from interlock.services.review_service import ReviewService
from interlock.services.scheduler_service import SchedulerService
from interlock.services.sharing_service import SharingService
from interlock.services.sheet_sync_service import SheetSyncService
from interlock.services.task_service import TaskService


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_clock(request: Request) -> Clock:
    return request.app.state.clock  # type: ignore[no-any-return]


def get_whatsapp(request: Request) -> WhatsAppProvider:
    return request.app.state.whatsapp  # type: ignore[no-any-return]


def get_sheets(request: Request) -> SpreadsheetProvider:
    return request.app.state.sheets  # type: ignore[no-any-return]


def get_session(request: Request) -> Iterator[Session]:
    """One transaction per request: commits if the handler returns normally,
    rolls back if it raises. This is what makes "task edit + audit entry
    land together" true at the HTTP layer, not just inside a single service
    method."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_current_actor(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    clock: Clock = Depends(get_clock),
    x_actor_id: str | None = Header(default=None),
    x_actor_name: str | None = Header(default=None),
) -> Actor:
    """Phase 1 has no login flow -- see Settings.default_user_id's docstring.

    Ensures the corresponding users row exists (approved_by_user_id is a real
    foreign key), idempotently, on every request rather than requiring a
    separate seed step someone can forget to run.
    """
    actor_id = x_actor_id or settings.default_user_id
    actor_name = x_actor_name or settings.default_user_name

    if session.get(UserRow, actor_id) is None:
        session.add(
            UserRow(
                id=actor_id,
                email=f"{actor_id}@local",
                display_name=actor_name,
                role="approver",
                is_active=True,
                created_at=clock.now(),
            )
        )
        session.flush()

    return Actor(kind=ActorKind.USER, id=actor_id, label=actor_name)


def get_task_repo(session: Session = Depends(get_session)) -> TaskRepository:
    return PostgresTaskRepository(session)


def get_audit(session: Session = Depends(get_session)) -> AuditSink:
    return PostgresAuditSink(session)


def get_approval_repo(session: Session = Depends(get_session)) -> ApprovalRepository:
    return ApprovalRepository(session)


def get_share_repo(session: Session = Depends(get_session)) -> ShareRepository:
    return ShareRepository(session)


def get_scheduler_repo(session: Session = Depends(get_session)) -> SchedulerRepository:
    return SchedulerRepository(session)


def get_group_repo(session: Session = Depends(get_session)) -> WhatsAppGroupRepository:
    return WhatsAppGroupRepository(session)


def get_sync_repo(session: Session = Depends(get_session)) -> SyncRepository:
    return SyncRepository(session)


def get_sequences(session: Session = Depends(get_session)) -> PostgresSequences:
    return PostgresSequences(session)


def get_task_service(
    repo: TaskRepository = Depends(get_task_repo),
    audit: AuditSink = Depends(get_audit),
    clock: Clock = Depends(get_clock),
) -> TaskService:
    return TaskService(repo, audit, clock)


def get_review_service(
    approval_repo: ApprovalRepository = Depends(get_approval_repo),
    task_repo: TaskRepository = Depends(get_task_repo),
    audit: AuditSink = Depends(get_audit),
    settings: Settings = Depends(get_settings_dep),
) -> ReviewService:
    return ReviewService(approval_repo, task_repo, audit, settings.tz)


def get_sharing_service(
    approval_repo: ApprovalRepository = Depends(get_approval_repo),
    share_repo: ShareRepository = Depends(get_share_repo),
    scheduler_repo: SchedulerRepository = Depends(get_scheduler_repo),
    sequences: PostgresSequences = Depends(get_sequences),
    task_service: TaskService = Depends(get_task_service),
    task_repo: TaskRepository = Depends(get_task_repo),
    audit: AuditSink = Depends(get_audit),
    settings: Settings = Depends(get_settings_dep),
) -> SharingService:
    return SharingService(
        approval_repo=approval_repo,
        share_repo=share_repo,
        scheduler_repo=scheduler_repo,
        sequences=sequences,
        task_service=task_service,
        task_repo=task_repo,
        audit=audit,
        tz=settings.tz,
        max_message_length=settings.max_message_length,
    )


def get_scheduler_service(
    scheduler_repo: SchedulerRepository = Depends(get_scheduler_repo),
    share_repo: ShareRepository = Depends(get_share_repo),
    group_repo: WhatsAppGroupRepository = Depends(get_group_repo),
    whatsapp: WhatsAppProvider = Depends(get_whatsapp),
    audit: AuditSink = Depends(get_audit),
    settings: Settings = Depends(get_settings_dep),
) -> SchedulerService:
    return SchedulerService(
        scheduler_repo=scheduler_repo,
        share_repo=share_repo,
        group_repo=group_repo,
        whatsapp=whatsapp,
        audit=audit,
        tz=settings.tz,
        send_grace=settings.send_grace,
        retry_delays=settings.retry_delays_seconds,
        worker_id="api",
        claim_lease=settings.claim_lease,
        claim_batch_size=settings.claim_batch_size,
        enforce_day_boundary=settings.enforce_day_boundary,
        strict_data_drift=settings.strict_data_drift,
    )


def get_sheet_sync_service(
    sheets: SpreadsheetProvider = Depends(get_sheets),
    sync_repo: SyncRepository = Depends(get_sync_repo),
    task_repo: TaskRepository = Depends(get_task_repo),
    audit: AuditSink = Depends(get_audit),
) -> SheetSyncService:
    return SheetSyncService(
        sheets=sheets, sync_repo=sync_repo, task_repo=task_repo, audit=audit
    )


def now_dep(clock: Clock = Depends(get_clock)) -> dt.datetime:
    return clock.now()
