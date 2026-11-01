"""Send one small optional chat check to the configured local Ollama model."""

from __future__ import annotations

from dataclasses import replace

from stockwise.agent.ollama import generate_explanation
from stockwise.api.config import get_settings


def main() -> int:
    """Check the configured model without involving the database or LangGraph."""
    settings = replace(get_settings(), llm_enabled=True)
    print(f"Ollama base URL: {settings.ollama_base_url}")
    print(f"Model: {settings.ollama_model or '(not configured)'}")

    result = generate_explanation(
        settings,
        question="Reply with the words StockWise local connection check passed.",
        evidence_facts={"purpose": "local connection check"},
    )
    if result.text is None:
        print("Ollama chat check: failed")
        print(f"Failure category: {result.fallback_reason}")
        return 1

    print("Ollama chat check: passed")
    print(f"Model: {result.model_name}")
    print(f"Response: {result.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
