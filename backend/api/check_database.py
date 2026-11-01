"""Safely diagnose the configured PostgreSQL connection from the project root."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from backend.api.config import get_settings
from backend.api.database import check_database_connection, create_database_engine


_URI_PASSWORD = re.compile(
    r"((?:postgres(?:ql)?(?:\+psycopg)?://)[^:/@\s]+:)[^@\s]+@",
    re.IGNORECASE,
)
_KEYWORD_PASSWORD = re.compile(
    r"(password\s*=\s*)(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)


def sanitize_driver_message(message: str, *, maximum_length: int = 500) -> str:
    """Remove URL and keyword passwords before displaying a driver message."""
    sanitized = _URI_PASSWORD.sub(r"\1***@", str(message))
    sanitized = _KEYWORD_PASSWORD.sub(r"\1***", sanitized)
    return sanitized[:maximum_length]


def _safe_error_details(error: SQLAlchemyError) -> dict[str, Any]:
    original_error = getattr(error, "orig", None) or error
    return {
        "driver_error_type": type(original_error).__name__,
        "sqlstate": getattr(original_error, "sqlstate", None),
        "message": sanitize_driver_message(str(original_error)),
    }


def main() -> int:
    settings = get_settings()
    if not settings.database_url:
        print("STOCKWISE_DATABASE_URL is missing from the process environment and .env.")
        return 1

    try:
        url = make_url(settings.database_url)
        if url.drivername != "postgresql+psycopg":
            print("Database URL must use the postgresql+psycopg driver.")
            return 1
        print(f"Configured database target: {url.render_as_string(hide_password=True)}")
    except SQLAlchemyError:
        print("Database URL could not be parsed. Check its scheme and URL-encode reserved password characters.")
        return 1

    engine = None
    try:
        engine = create_database_engine(settings.database_url)
        check_database_connection(engine)
    except (SQLAlchemyError, ValueError) as error:
        print("Database connection: FAILED")
        if isinstance(error, SQLAlchemyError):
            details = _safe_error_details(error)
            print(f"Driver error type: {details['driver_error_type']}")
            print(f"SQLSTATE: {details['sqlstate'] or 'none'}")
            print(f"Sanitized driver message: {details['message']}")
        else:
            print("Database URL configuration is invalid for the PostgreSQL driver.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print("Database connection: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
