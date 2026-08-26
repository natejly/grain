from __future__ import annotations

from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import engine_from_config, pool

from alembic import context
from app import models  # noqa: F401
from app.config import get_settings
from app.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata

# Alembic hardcodes alembic_version.version_num as VARCHAR(32). Three revision
# ids in this tree are longer — the longest, 0014_memory_supersession_and_chunk
# _vectors, is 42 characters — so stamping one raises
# StringDataRightTruncation and leaves the upgrade half-applied.
#
# It has never bitten locally because development runs on SQLite, which does
# not enforce VARCHAR lengths; PostgreSQL does, so the failure only appears on
# a real deployment. (It did: the first production migration, 2026-08-26.)
# Widening the column here fixes every database rather than renaming
# already-applied revisions out from under existing ones.
_VERSION_NUM_WIDTH = 128


def _widen_version_table(connection: sa.Connection) -> None:
    """Ensure alembic_version.version_num fits this tree's revision ids.

    Runs before context.run_migrations() so the table is already wide by the
    time Alembic writes to it — including on a fresh database, where Alembic
    would otherwise create it at VARCHAR(32) itself.
    """
    if connection.dialect.name == "sqlite":
        # No length enforcement, and no ALTER COLUMN TYPE to apply anyway.
        return

    inspector = sa.inspect(connection)
    if not inspector.has_table("alembic_version"):
        # Pre-create it exactly as Alembic would, but wide. Alembic finds the
        # existing table and uses it instead of creating a narrow one.
        connection.execute(
            sa.text(
                "CREATE TABLE alembic_version ("
                f"version_num VARCHAR({_VERSION_NUM_WIDTH}) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        return

    for column in inspector.get_columns("alembic_version"):
        if column["name"] != "version_num":
            continue
        width = getattr(column["type"], "length", None)
        if width is not None and width < _VERSION_NUM_WIDTH:
            connection.execute(
                sa.text(
                    "ALTER TABLE alembic_version ALTER COLUMN version_num "
                    f"TYPE VARCHAR({_VERSION_NUM_WIDTH})"
                )
            )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        _widen_version_table(connection)
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
