"""Deprecated command retained only to explain the local-only provider policy."""

from __future__ import annotations

from typing import Any

from stockwise.api.config import Settings, get_settings


def check_omniroute(
    _settings: Settings,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Return a fixed disabled result without attempting a network request."""
    return {"ok": False, "failure_category": "provider_disabled_local_only"}


def main() -> int:
    get_settings()
    print("OmniRoute is disabled. StockWise AI inference uses local Ollama only.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
