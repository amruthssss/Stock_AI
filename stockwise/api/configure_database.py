"""Interactively write a local PostgreSQL URL to the project's ignored .env file."""

from __future__ import annotations

from getpass import getpass
from pathlib import Path
from typing import Callable

from dotenv import set_key
from sqlalchemy.engine import URL

from stockwise.api.config import PROJECT_ROOT


def build_database_url(password: str) -> str:
    """Build a URL for the documented local StockWise PostgreSQL setup."""
    if not password:
        raise ValueError("The database password cannot be empty.")

    url = URL.create(
        drivername="postgresql+psycopg",
        username="stockwise_user",
        password=password,
        host="127.0.0.1",
        port=5432,
        database="stockwise",
    )
    return url.render_as_string(hide_password=False)


def configure_database(
    env_path: Path = PROJECT_ROOT / ".env",
    password_reader: Callable[[str], str] | None = None,
) -> None:
    """Prompt for a matching password and update only STOCKWISE_DATABASE_URL."""
    read_password = password_reader or getpass
    password = read_password("PostgreSQL password for stockwise_user: ")
    confirmation = read_password("Enter the same password again: ")

    if not password:
        raise ValueError("The database password cannot be empty.")
    if password != confirmation:
        raise ValueError("The passwords did not match; .env was not changed.")

    env_path = Path(env_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    set_key(
        str(env_path),
        "STOCKWISE_DATABASE_URL",
        build_database_url(password),
        quote_mode="always",
    )


def main() -> int:
    try:
        configure_database()
    except (EOFError, KeyboardInterrupt):
        print("Configuration cancelled; .env was not changed.")
        return 1
    except ValueError as error:
        print(f"Configuration failed: {error}")
        return 1

    print("Saved the local database URL to .env. The password was not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
