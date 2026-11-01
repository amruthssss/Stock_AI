"""Forecast Intelligence specialist entry point."""

from __future__ import annotations

from typing import Any

from stockwise.agent.specialists import run_specialist


def run_forecast_agent(**kwargs: Any) -> dict[str, Any]:
    return run_specialist("forecast_intelligence", **kwargs)
