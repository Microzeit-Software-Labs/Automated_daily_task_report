"""Alembic environment.

Two things this deliberately does differently from the generated template:

1. The connection URL comes from ``MIGRATION_DATABASE_URL`` in ``.env``, read
   directly rather than through :class:`interlock.config.Settings`. The
   application's settings model only ever sees the low-privilege app role's
   URL (``DATABASE_URL``); migrations run as the schema owner, and that
   distinction only means something if the two credentials never mix through a
   shared code path.

2. ``target_metadata`` points at the real ``Base.metadata`` populated by
   importing ``interlock.adapters.persistence.models`` -- so ``--autogenerate``
   diffs against what the ORM models actually declare, not against nothing.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from interlock.adapters.persistence import models  # noqa: F401  -- populates Base.metadata
from interlock.adapters.persistence.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

load_dotenv()
migration_url = os.environ.get("MIGRATION_DATABASE_URL")
if not migration_url:
    raise RuntimeError(
        "MIGRATION_DATABASE_URL is not set. Run scripts/setup-database.ps1 first, "
        "or set it directly in .env."
    )
config.set_main_option("sqlalchemy.url", migration_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
