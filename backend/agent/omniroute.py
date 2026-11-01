"""Retired OmniRoute adapter retained as a no-network compatibility shim."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExplanationResult:
    """Legacy result shape; this provider is permanently disabled."""

    text: str | None
    model_name: str | None
    fallback_reason: str | None


def generate_explanation(*_args: Any, **_kwargs: Any) -> ExplanationResult:
    """Never send external requests; StockWise inference now uses local Ollama only."""
    return ExplanationResult(None, None, "provider_disabled_local_only")
