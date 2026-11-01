"""Small, local-first API settings loaded from the process environment or .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from sqlalchemy.engine import URL

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    """Runtime settings that do not expose credentials in representations."""

    database_url: str | None = field(default=None, repr=False)
    llm_enabled: bool = False
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str | None = None
    agent_execution_mode: Literal["sequential", "parallel"] = "sequential"
    approval_admin_token: str | None = field(default=None, repr=False)
    # Preserve the prior programmatic default; get_settings uses a longer local-model timeout.
    llm_timeout_seconds: float = 12.0
    integration_api_key: str | None = field(default=None, repr=False)
    integration_rate_limit_per_minute: int = 60
    n8n_base_url: str = "http://127.0.0.1:5678/api/v1"
    n8n_api_key_file: Path | None = field(default=None, repr=False)
    n8n_timeout_seconds: float = 8.0


def get_settings(project_root: Path = PROJECT_ROOT) -> Settings:
    """Load local settings, preferring environment values over the project .env file."""
    load_dotenv(Path(project_root) / ".env", override=False)
    raw_url = os.getenv("STOCKWISE_DATABASE_URL", "").strip()
    if not raw_url:
        raw_url = _database_url_from_components()
    enabled_value = os.getenv("STOCKWISE_LLM_ENABLED", "false").strip().lower()
    return Settings(
        database_url=raw_url or None,
        llm_enabled=enabled_value in {"1", "true", "yes", "on"},
        ollama_base_url=(
            os.getenv("STOCKWISE_OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip()
            or "http://127.0.0.1:11434"
        ),
        ollama_model=os.getenv("STOCKWISE_OLLAMA_MODEL", "").strip() or None,
        agent_execution_mode=(
            "parallel"
            if os.getenv("STOCKWISE_AGENT_EXECUTION_MODE", "sequential").strip().lower() == "parallel"
            else "sequential"
        ),
        approval_admin_token=os.getenv("STOCKWISE_APPROVAL_ADMIN_TOKEN", "").strip() or None,
        llm_timeout_seconds=_read_timeout_seconds(),
        integration_api_key=_integration_api_key(),
        integration_rate_limit_per_minute=_read_rate_limit(),
        n8n_base_url=(
            os.getenv("STOCKWISE_N8N_BASE_URL", "http://127.0.0.1:5678/api/v1").strip()
            or "http://127.0.0.1:5678/api/v1"
        ),
        n8n_api_key_file=_n8n_api_key_file(),
        n8n_timeout_seconds=_read_n8n_timeout_seconds(),
    )


def get_database_url(project_root: Path = PROJECT_ROOT) -> str:
    """Resolve the shared database URL for API, workers, and Alembic."""
    database_url = get_settings(project_root).database_url
    if not database_url:
        raise RuntimeError(
            "Database connection is not configured in STOCKWISE_DATABASE_URL "
            "or STOCKWISE_DATABASE_* components"
        )
    return database_url


def _database_url_from_components() -> str | None:
    """Build a database URL without interpolating a password into URL syntax."""
    password = os.getenv("STOCKWISE_DATABASE_PASSWORD")
    host = os.getenv("STOCKWISE_DATABASE_HOST", "").strip()
    if password is None or not host:
        return None
    try:
        port = int(os.getenv("STOCKWISE_DATABASE_PORT", "5432"))
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    url = URL.create(
        "postgresql+psycopg",
        username=os.getenv("STOCKWISE_DATABASE_USER", "stockwise").strip() or "stockwise",
        password=password,
        host=host,
        port=port,
        database=os.getenv("STOCKWISE_DATABASE_NAME", "stockwise").strip() or "stockwise",
    )
    return url.render_as_string(hide_password=False)


def _integration_api_key() -> str | None:
    """Load the dedicated integration key from the environment or a secret file."""
    environment_key = os.getenv("STOCKWISE_INTEGRATION_API_KEY", "").strip()
    if environment_key:
        return environment_key
    secret_file = os.getenv("STOCKWISE_INTEGRATION_API_KEY_FILE", "").strip()
    if not secret_file:
        return None
    try:
        file_key = Path(secret_file).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return file_key or None


def _n8n_api_key_file() -> Path | None:
    """Resolve the n8n API key file path without reading the secret at startup."""
    configured_path = os.getenv("STOCKWISE_N8N_API_KEY_FILE", "").strip()
    if configured_path:
        return Path(configured_path)
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "StockWise" / "secrets" / "n8n_api_key"
    return None


def _read_n8n_timeout_seconds() -> float:
    """Keep n8n requests short so a down local service cannot stall API pages."""
    try:
        value = float(os.getenv("STOCKWISE_N8N_TIMEOUT_SECONDS", "8"))
    except ValueError:
        return 8.0
    return value if 1 <= value <= 30 else 8.0


def _read_timeout_seconds() -> float:
    """Use a bounded local-inference timeout; malformed values fall back safely."""
    try:
        value = float(os.getenv("STOCKWISE_LLM_TIMEOUT_SECONDS", "120"))
    except ValueError:
        return 120.0
    if not 1 <= value <= 300:
        return 120.0
    return value


def _read_rate_limit() -> int:
    try:
        value = int(os.getenv("STOCKWISE_INTEGRATION_RATE_LIMIT_PER_MINUTE", "60"))
    except ValueError:
        return 60
    return value if 1 <= value <= 600 else 60
