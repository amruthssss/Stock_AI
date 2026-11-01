"""Inventory Risk specialist entry point."""

from __future__ import annotations

from typing import Any

from stockwise.agent.specialists import run_specialist


def run_inventory_risk_agent(**kwargs: Any) -> dict[str, Any]:
    return run_specialist("inventory_risk", **kwargs)
