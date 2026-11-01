"""Shared FastAPI dependencies for read-only database routes."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy import Engine

from backend.api.errors import DatabaseUnavailableError


def get_database_engine(request: Request) -> Engine:
    """Return the configured engine or fail with a handled unavailable error."""
    if request.app.state.database_configuration_error:
        raise DatabaseUnavailableError("Database configuration is invalid")
    engine = request.app.state.database_engine
    if engine is None:
        raise DatabaseUnavailableError("Database connection is not configured")
    return engine
