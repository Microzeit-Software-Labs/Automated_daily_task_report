"""The error envelope.

Every ``DomainError`` becomes a JSON body of the same shape, with the stable
``code`` the frontend switches on -- see ``domain/common/errors.py`` for why
that string, not the HTTP status, is the contract.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from interlock.domain.common.errors import DomainError


async def domain_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)  # registered only for this type below
    return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, domain_error_handler)
