"""HTTP-level idempotency: same ``Idempotency-Key``, same outcome.

This is layer 1 of the two-layer idempotency the architecture calls for.
Layer 2 -- the ``UNIQUE(approval_request_id, action_version)`` constraint on
``share_jobs`` -- is what actually *proves* Scenario 6 under real concurrency
(see the 50-thread test in ``test_sharing_service.py``); this middleware is
what stops a client's blind retry from re-executing a request's side effects
at all, rather than relying on every downstream operation being safe to
repeat.

Only applied to mutating requests that opt in by sending the header --
GETs are naturally idempotent and never need this, and a POST without the
header is simply not protected (the client did not ask for it).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from interlock.adapters.persistence.models import IdempotencyKeyRow
from interlock.domain.common.clock import Clock

_PROTECTED_PATH_MARKERS: tuple[str, ...] = ("/commit", "/bulk-update")
"""A request only needs idempotency protection if it can create a
side effect that duplicating would actually harm -- share jobs and bulk task
writes. Plain POST /tasks (a single create) is naturally safe to retry with a
fresh id each time and is left unprotected by design."""


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: FastAPI,
        *,
        session_factory: sessionmaker[Session],
        clock: Clock,
        ttl_hours: int,
    ) -> None:
        super().__init__(app)
        self._session_factory = session_factory
        self._clock = clock
        self._ttl_hours = ttl_hours

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        key = request.headers.get("Idempotency-Key")
        if (
            not key
            or request.method != "POST"
            or not any(marker in request.url.path for marker in _PROTECTED_PATH_MARKERS)
        ):
            return await call_next(request)

        body = await request.body()
        request_hash = hashlib.sha256(body).hexdigest()

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive  # re-feed the body we already consumed above

        now = self._clock.now()
        session = self._session_factory()
        try:
            existing = session.execute(
                select(IdempotencyKeyRow).where(IdempotencyKeyRow.key == key)
            ).scalar_one_or_none()

            if existing is not None:
                if existing.request_hash != request_hash:
                    return JSONResponse(
                        status_code=422,
                        content={
                            "error": {
                                "code": "IDEMPOTENCY_KEY_REUSED",
                                "message": (
                                    f"Idempotency-Key {key!r} was already used for a "
                                    "different request body."
                                ),
                                "details": {"key": key},
                                "retryable": False,
                            }
                        },
                    )
                if existing.state == "COMPLETED":
                    return JSONResponse(
                        status_code=existing.response_status or 200,
                        content=existing.response_body,
                    )
                # IN_PROGRESS: a genuine concurrent duplicate arrived before the
                # first request finished. Phase 1 does not implement a
                # wait-for-completion path here -- it proceeds, and the
                # downstream database constraints (share_jobs' own uniqueness,
                # notably) are the real backstop for that narrower race.
            else:
                session.add(
                    IdempotencyKeyRow(
                        key=key,
                        endpoint=request.url.path,
                        request_hash=request_hash,
                        state="IN_PROGRESS",
                        created_at=now,
                        expires_at=now + dt.timedelta(hours=self._ttl_hours),
                    )
                )
                session.commit()
        finally:
            session.close()

        response = await call_next(request)

        body_chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
        response_bytes = b"".join(body_chunks)

        cache_session = self._session_factory()
        try:
            row = cache_session.get(IdempotencyKeyRow, key)
            if row is not None:
                row.state = "COMPLETED"
                row.response_status = response.status_code
                row.response_body = json.loads(response_bytes) if response_bytes else None
                cache_session.commit()
        finally:
            cache_session.close()

        return Response(
            content=response_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
