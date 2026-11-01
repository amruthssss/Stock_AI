"""Alembic environment using the same local settings as the API."""

from __future__ import annotations

from alembic import context
from stockwise.api.config import get_database_url
from stockwise.api.database import create_database_engine
from stockwise.persistence.models import Base


config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    return get_database_url()


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations through the configured SQLAlchemy engine."""
    engine = create_database_engine(_database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
