"""Local stdio MCP server for approved, read-only StockWise evidence tools."""

from __future__ import annotations

import logging
from typing import Any, Literal

from mcp.server import MCPServer
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from stockwise.agent.investigation import read_forecast_run, read_inventory_risk_run
from stockwise.api.config import get_settings
from stockwise.api.database import create_database_engine


logger = logging.getLogger(__name__)


def create_mcp_server(engine: Engine | None = None) -> MCPServer:
    """Create an MCP server with exactly two database-backed, read-only tools.

    An engine can be injected by tests. In production, the local database engine is
    constructed lazily from STOCKWISE_DATABASE_URL when a tool is first called.
    """
    database_engine = engine
    server = MCPServer(
        "StockWise Read Only",
        instructions=(
            "Use these tools only to read persisted StockWise forecast and inventory-risk "
            "evidence. They never run simulations, make purchases, or change prices. "
            "Inventory-risk records use synthetic operational assumptions."
        ),
    )

    def get_engine() -> Engine:
        nonlocal database_engine
        if database_engine is not None:
            return database_engine
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("Database configuration is missing")
        database_engine = create_database_engine(settings.database_url)
        return database_engine

    @server.tool()
    def read_forecast_evidence(
        run_id: str,
        evaluation_period: Literal["rolling_origin", "held_out"] = "held_out",
    ) -> dict[str, Any]:
        """Read saved forecast metrics for one run and chronological evaluation period."""
        if not run_id or len(run_id) > 64:
            return {"status": "invalid_input", "message": "run_id must be 1 to 64 characters."}
        try:
            result = read_forecast_run(
                get_engine(), run_id=run_id, evaluation_period=evaluation_period
            )
        except (RuntimeError, SQLAlchemyError):
            logger.warning("MCP forecast evidence read failed; database details suppressed")
            return {"status": "unavailable", "message": "Saved forecast evidence is unavailable."}
        return {
            "status": result["status"],
            "evidence": result.get("evidence", []),
            "tool_history": result.get("tool_history", []),
        }

    @server.tool()
    def read_inventory_risk_evidence(run_id: str) -> dict[str, Any]:
        """Read saved inventory-risk summary and label its synthetic data provenance."""
        if not run_id or len(run_id) > 64:
            return {"status": "invalid_input", "message": "run_id must be 1 to 64 characters."}
        try:
            result = read_inventory_risk_run(get_engine(), run_id=run_id)
        except (RuntimeError, SQLAlchemyError):
            logger.warning("MCP inventory-risk evidence read failed; database details suppressed")
            return {
                "status": "unavailable",
                "message": "Saved inventory-risk evidence is unavailable.",
            }
        return {
            "status": result["status"],
            "evidence": result.get("evidence", []),
            "tool_history": result.get("tool_history", []),
        }

    return server


mcp = create_mcp_server()


def main() -> None:
    """Run the MCP server over stdio for a local MCP host to launch."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
