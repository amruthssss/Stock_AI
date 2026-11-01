"""SQLAlchemy engine setup and a minimal PostgreSQL connectivity probe."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url


def create_database_engine(database_url: str) -> Engine:
    """Create a synchronous SQLAlchemy engine using Psycopg 3.

    The explicit driver name prevents SQLAlchemy from silently selecting the
    legacy psycopg2 driver. Connections are checked before pool reuse and a
    connection attempt is bounded to three seconds.
    """
    url = make_url(database_url)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("STOCKWISE_DATABASE_URL must use postgresql+psycopg")
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def check_database_connection(engine: Engine) -> None:
    """Run a read-only query to verify that PostgreSQL accepts connections."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
