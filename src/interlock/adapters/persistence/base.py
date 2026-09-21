"""SQLAlchemy wiring.

This module and its siblings under ``adapters/persistence`` are the *only*
place SQL exists in this codebase. The domain layer never imports SQLAlchemy;
these classes are the boundary where domain entities become rows and back.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Shared declarative base for every mapped table."""


def make_engine(database_url: str | URL, *, echo: bool = False, **engine_kwargs: Any) -> Engine:
    # Accepts a URL object as well as a plain string specifically so a caller
    # that built the URL via URL.set(...) (e.g. swapping the target database
    # while keeping a role's credentials) can pass it straight through --
    # str(url_object) would silently mask the password (see
    # tests/integration/conftest.py's app_role_engine for exactly this trap).
    #
    # **engine_kwargs passes through to create_engine() (e.g. pool_size,
    # max_overflow, poolclass) so a caller with unusual connection-volume
    # needs -- a concurrency stress test opening 50 connections at once,
    # notably -- can widen the pool without a second, divergent engine
    # construction path that would skip the UTC pinning below.
    engine = create_engine(
        database_url, echo=echo, pool_pre_ping=True, future=True, **engine_kwargs
    )

    @event.listens_for(engine, "connect")
    def _force_utc_session(dbapi_connection: Any, _connection_record: Any) -> None:
        # PostgreSQL's timestamptz stores a UTC instant but converts to/from
        # the *client session's* TimeZone setting on every read and write.
        # That setting defaults to the OS locale of whatever machine is
        # connecting -- on this machine, Asia/Calcutta -- so without pinning
        # it, a value written as 17:04 UTC reads back as 22:34+05:30. Same
        # instant, different ISO string, which silently breaks anything that
        # hashes or string-compares a timestamp (the audit chain's
        # entry_hash, most consequentially). Every connection this engine
        # ever opens gets pinned to UTC before any application code touches
        # it, so correctness never depends on which machine happens to be
        # running the process.
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET TIME ZONE 'UTC'")
        finally:
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def unit_of_work(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """One transaction, committed on success, rolled back on any exception.

    This is where the "audit entry in the same transaction as the change it
    describes" guarantee actually lives: callers do their work and write their
    audit row inside this block, and either both land or neither does.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
