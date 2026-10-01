"""Integration test fixtures.

These tests run against a real PostgreSQL -- ``TEST_DATABASE_URL`` in ``.env``,
a database entirely separate from the development one. The schema is created
once per test session by running the *real* Alembic migrations against it (not
``Base.metadata.create_all()``, which would skip the hand-written ``REVOKE``
statements this suite exists to verify).

Each test runs inside its own transaction, rolled back afterwards, so tests
never see each other's data and the schema is not recreated per test. Tests
use ``session.flush()`` rather than ``session.commit()`` to trigger a
constraint check without ending that transaction.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from interlock.adapters.persistence.base import make_engine, make_session_factory

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL is not set. Run scripts/setup-database.ps1 to "
            "provision the test database, or set it directly in .env."
        )
    return url


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """The test database, migrated to head exactly once for the whole run."""
    url = _test_database_url()

    # Migrations normally read MIGRATION_DATABASE_URL from the environment via
    # migrations/env.py; here they must target the *test* database instead, so
    # the URL is overridden explicitly rather than relying on that env var.
    os.environ["MIGRATION_DATABASE_URL"] = url
    alembic_cfg = Config(_REPO_ROOT / "alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_cfg, "head")

    # make_engine(), not a bare create_engine(): this is what pins every
    # connection's session timezone to UTC (see its docstring) -- tests must
    # go through the same engine construction production code does, or a
    # timezone/serialization bug like the one that motivated that fix could
    # pass here while still being broken for real.
    engine = make_engine(url)

    # Belt and suspenders: most tests isolate themselves via a rolled-back
    # transaction (see db_session below), but a test that deliberately proves
    # real commit/rollback behaviour needs its own real transaction -- and a
    # bug in one of those (this project has already hit exactly this once)
    # leaves permanent rows that silently poison every later run's whole-table
    # checks, like the audit chain's load_and_verify_chain(). Starting every
    # session from a genuinely empty schema means that class of bug fails
    # loudly in the run that causes it, instead of haunting every run after.
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE "
                "share_recipients, share_jobs, report_snapshots, task_history, "
                "tasks, approval_requests, scheduled_actions, whatsapp_groups, "
                "idempotency_keys, audit_logs, users, "
                "whatsapp_agent_commands, whatsapp_agent_status, sheet_source "
                "RESTART IDENTITY CASCADE"
            )
        )

    yield engine
    engine.dispose()


@pytest.fixture
def agent_session_factory(migrated_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """For the WhatsApp agent tests, which commit for real: the provider and
    the FakeAgent use separate connections, so the rolled-back ``db_session``
    (which nothing outside its own transaction can see) doesn't work.

    Truncating only *before* the test is not enough -- a row left behind (most
    consequentially the single-row whatsapp_agent_status) would collide with
    whatever the next test, in any file, tries to insert. Truncate on both
    sides."""

    def _truncate() -> None:
        with migrated_engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE whatsapp_agent_commands, whatsapp_agent_status"))

    _truncate()
    yield make_session_factory(migrated_engine)
    _truncate()


@pytest.fixture
def db_connection(migrated_engine: Engine) -> Iterator[Connection]:
    """A connection with an open transaction, rolled back after the test."""
    connection = migrated_engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def db_session(db_connection: Connection) -> Iterator[Session]:
    """An ORM session bound to the same rolled-back transaction.

    Use ``session.flush()`` to trigger an INSERT/UPDATE and surface a
    constraint violation without committing -- committing would end the
    transaction this fixture is about to roll back.
    """
    factory = sessionmaker(bind=db_connection, future=True, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="session")
def app_role_engine(migrated_engine: Engine) -> Iterator[Engine]:
    """A connection to the test database as ``interlock_app`` -- the same
    low-privilege role the running application uses -- not as the migration
    owner every other fixture connects as.

    Built by taking the app role's real credentials from ``DATABASE_URL`` (the
    development database) and pointing them at the test database's name
    instead, rather than requiring a second provisioning step. The app role
    already has USAGE/CONNECT and default-privilege grants on both databases
    from ``scripts/setup-database.ps1``.

    Exists specifically to prove the audit log and report snapshots are
    immutable *as the application actually connects*, not merely as an owner
    role that could bypass the restriction anyway.
    """
    app_url = os.environ.get("DATABASE_URL")
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not app_url or not test_url:
        pytest.skip("DATABASE_URL and TEST_DATABASE_URL must both be set.")

    combined = make_url(app_url).set(database=make_url(test_url).database)
    # make_engine() takes the URL object directly rather than str(combined):
    # URL.__str__ calls render_as_string(hide_password=True) by design, so
    # stringifying it here would silently hand it the literal masked text
    # "***" as the password instead of the real one.
    engine = make_engine(combined)
    yield engine
    engine.dispose()
