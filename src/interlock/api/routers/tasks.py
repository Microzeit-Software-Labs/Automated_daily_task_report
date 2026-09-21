"""Task CRUD.

``PATCH`` requires ``If-Match`` -- the version the caller believes they are
editing -- which becomes the ``expected_version`` the repository's atomic
conditional UPDATE checks. A mismatch is a real, current version conflict, not
a guess: see ``PostgresTaskRepository.update``'s docstring for why the check
has to be atomic rather than fetch-then-compare.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from interlock.api import deps
from interlock.api.schemas import (
    BulkUpdateItemResult,
    BulkUpdateRequest,
    BulkUpdateResponse,
    TaskCreateRequest,
    TaskEditFields,
    TaskOut,
)
from interlock.domain.common.actor import Actor
from interlock.domain.common.errors import NotFoundError, ValidationFailedError
from interlock.domain.ports.repositories import TaskFilter
from interlock.domain.tasks.entities import Priority, TaskStatus
from interlock.services.task_service import BulkUpdateItem, TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskOut])
def list_tasks(
    status_filter: list[TaskStatus] | None = Query(default=None, alias="status"),
    priority_filter: list[Priority] | None = Query(default=None, alias="priority"),
    owner_id: str | None = None,
    project_id: str | None = None,
    search: str | None = None,
    service: TaskService = Depends(deps.get_task_service),
) -> list[TaskOut]:
    criteria = TaskFilter(
        statuses=frozenset(status_filter) if status_filter else None,
        priorities=frozenset(priority_filter) if priority_filter else None,
        owner_id=owner_id,
        project_id=project_id,
        search=search,
    )
    return [TaskOut.from_entity(t) for t in service.list_tasks(criteria)]


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: str, service: TaskService = Depends(deps.get_task_service)) -> TaskOut:
    task = service.get(task_id) or service.get_by_display_id(task_id)
    if task is None:
        raise NotFoundError(f"Task {task_id} does not exist.", task_id=task_id)
    return TaskOut.from_entity(task)


@router.post("", response_model=TaskOut, status_code=201)
def create_task(
    payload: TaskCreateRequest,
    actor: Actor = Depends(deps.get_current_actor),
    service: TaskService = Depends(deps.get_task_service),
) -> TaskOut:
    task = service.create(
        title=payload.title,
        actor=actor,
        description=payload.description,
        project_id=payload.project_id,
        owner_id=payload.owner_id,
        priority=payload.priority,
        status=payload.status,
        due_date=payload.due_date,
        remarks=payload.remarks,
        tags=payload.tags,
    )
    return TaskOut.from_entity(task)


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(
    task_id: str,
    payload: TaskEditFields,
    if_match: str = Header(..., alias="If-Match"),
    actor: Actor = Depends(deps.get_current_actor),
    service: TaskService = Depends(deps.get_task_service),
) -> TaskOut:
    try:
        expected_version = int(if_match.strip('"'))
    except ValueError as exc:
        raise ValidationFailedError(
            'If-Match must be an integer version, e.g. If-Match: "7".', if_match=if_match
        ) from exc

    changes = payload.model_dump(exclude_unset=True)
    task = service.update(task_id, changes, expected_version=expected_version, actor=actor)
    return TaskOut.from_entity(task)


@router.post("/bulk-update")
def bulk_update_tasks(
    payload: BulkUpdateRequest,
    actor: Actor = Depends(deps.get_current_actor),
    service: TaskService = Depends(deps.get_task_service),
) -> JSONResponse:
    items = [
        BulkUpdateItem(
            task_id=item.task_id,
            changes=item.changes.model_dump(exclude_unset=True),
            expected_version=item.expected_version,
        )
        for item in payload.items
    ]
    results = service.bulk_update(items, actor=actor)
    body = BulkUpdateResponse(
        results=[
            BulkUpdateItemResult(
                task_id=r.task_id,
                ok=r.ok,
                task=TaskOut.from_entity(r.task) if r.task else None,
                error_code=r.error_code,
                error_message=r.error_message,
            )
            for r in results
        ]
    )

    # 207 for a genuine mix of success and failure; 200 if everything
    # succeeded (the common case, and what most clients expect to check for
    # simply); 200 is also used if everything failed -- the per-item error
    # codes in the body say why, and no single HTTP status code represents
    # "all of these failed for different reasons" more usefully than 200 does.
    oks = [r.ok for r in results]
    status_code = 207 if oks and any(oks) and not all(oks) else 200
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))
