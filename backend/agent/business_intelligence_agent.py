"""Cross-domain Business Intelligence specialist entry point."""

from __future__ import annotations

from typing import Any

from backend.agent.specialists import run_specialist


def run_business_intelligence_agent(**kwargs: Any) -> dict[str, Any]:
    return run_specialist("business_intelligence", **kwargs)
