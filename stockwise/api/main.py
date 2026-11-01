"""FastAPI application with liveness and PostgreSQL readiness checks."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import ExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from stockwise.agent.checkpointing import enter_investigation_checkpointer
from stockwise.api.approval_routes import router as approval_router
from stockwise.api.config import Settings, get_settings
from stockwise.api.database import check_database_connection, create_database_engine
from stockwise.api.decision_routes import router as decision_lab_router
from stockwise.api.errors import DatabaseUnavailableError
from stockwise.api.forecast_routes import router as forecast_router
from stockwise.api.integration_routes import router as integration_router
from stockwise.api.integration_security import IntegrationRateLimiter
from stockwise.api.inventory_risk_routes import router as inventory_risk_router
from stockwise.api.investigation_routes import router as investigation_router
from stockwise.api.jobs_routes import router as jobs_router
from stockwise.api.n8n_client import N8nIntegrationError
from stockwise.api.n8n_routes import router as n8n_router
from stockwise.api.report_routes import router as reports_router

logger = logging.getLogger(__name__)
DatabaseProbe = Callable[[Engine], None]


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any,
    response_headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    headers = {"X-Request-ID": request_id}
    if response_headers:
        headers.update(response_headers)
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "details": details,
            "request_id": request_id,
        },
        headers=headers,
    )


def _database_failure_details(error: SQLAlchemyError) -> dict[str, str | None]:
    """Return only safe driver diagnostics, never the exception text or DSN."""
    original_error = getattr(error, "orig", None)
    sqlstate = getattr(original_error, "sqlstate", None)
    safe_text = str(original_error).lower() if original_error is not None else ""
    if isinstance(sqlstate, str) and sqlstate.startswith("28"):
        category = "authentication_rejected"
    elif sqlstate == "3D000":
        category = "database_not_found"
    elif isinstance(sqlstate, str) and sqlstate.startswith("08"):
        category = "connection_failed"
    elif "password authentication failed" in safe_text or "no password supplied" in safe_text:
        category = "authentication_rejected"
    elif "database" in safe_text and "does not exist" in safe_text:
        category = "database_not_found"
    elif "connection refused" in safe_text:
        category = "connection_refused"
    elif "could not translate host name" in safe_text or "name or service not known" in safe_text:
        category = "host_resolution_failed"
    elif "timeout expired" in safe_text or "connection timed out" in safe_text:
        category = "connection_timeout"
    elif "no route to host" in safe_text:
        category = "network_unreachable"
    elif sqlstate:
        category = "postgres_error"
    else:
        category = "connection_or_driver_error"
        sqlstate = None
    driver_error_type = type(original_error).__name__ if original_error is not None else type(error).__name__
    return {
        "database": "unavailable",
        "failure_category": category,
        "sqlstate": sqlstate,
        "driver_error_type": driver_error_type,
    }


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    database_probe: DatabaseProbe = check_database_connection,
) -> FastAPI:
    """Build the application; injectable settings and probe support offline tests."""
    settings = settings or get_settings()
    database_engine = engine
    configuration_error = False
    if database_engine is None and settings.database_url:
        try:
            database_engine = create_database_engine(settings.database_url)
        except (SQLAlchemyError, ValueError):
            # Keep the service importable and return a safe readiness error.
            configuration_error = True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            with ExitStack() as stack:
                app.state.investigation_checkpointer = enter_investigation_checkpointer(
                    app.state.database_engine, stack
                )
                yield
        finally:
            app_engine = app.state.database_engine
            if app_engine is not None:
                app_engine.dispose()

    app = FastAPI(
        title="StockWise API",
        version="0.1.0",
        description="Local-first, evidence-backed inventory decision support.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database_engine = database_engine
    app.state.database_probe = database_probe
    app.state.database_configuration_error = configuration_error
    app.state.investigation_checkpointer = None
    app.state.integration_rate_limiter = IntegrationRateLimiter()
    app.include_router(forecast_router)
    app.include_router(inventory_risk_router)
    app.include_router(investigation_router)
    app.include_router(approval_router)
    app.include_router(decision_lab_router)
    app.include_router(jobs_router)
    app.include_router(n8n_router)
    app.include_router(integration_router)
    app.include_router(reports_router)

    @app.middleware("http")
    async def assign_request_id(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, error: StarletteHTTPException):
        return _error_response(
            request,
            status_code=error.status_code,
            code="HTTP_ERROR",
            message="The request could not be completed.",
            details={"status_code": error.status_code},
            response_headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError):
        safe_errors = [
            {"location": list(item.get("loc", ())), "type": item.get("type", "invalid")}
            for item in error.errors()
        ]
        return _error_response(
            request,
            status_code=422,
            code="REQUEST_VALIDATION_FAILED",
            message="The request contains invalid fields.",
            details={"errors": safe_errors},
        )

    @app.exception_handler(DatabaseUnavailableError)
    async def handle_database_not_configured(request: Request, _error: DatabaseUnavailableError):
        return _error_response(
            request,
            status_code=503,
            code="DATABASE_UNAVAILABLE",
            message="The database is not available for this request.",
            details={"database": "unavailable"},
        )

    @app.exception_handler(N8nIntegrationError)
    async def handle_n8n_error(request: Request, error: N8nIntegrationError):
        """Expose only a safe error category, never an upstream response body."""
        return _error_response(
            request,
            status_code=error.status_code,
            code=f"N8N_{error.category.upper()}",
            message=error.message,
            details={"n8n": "unavailable", "failure_category": error.category},
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_query_error(request: Request, error: SQLAlchemyError):
        details = _database_failure_details(error)
        logger.warning(
            "PostgreSQL query failed: category=%s sqlstate=%s",
            details["failure_category"],
            details["sqlstate"] or "none",
        )
        return _error_response(
            request,
            status_code=503,
            code="DATABASE_UNAVAILABLE",
            message="PostgreSQL could not complete the request.",
            details=details,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception):
        # Log only the exception type. Database exception text can contain DSNs.
        logger.error("Unhandled API error of type %s", type(error).__name__)
        return _error_response(
            request,
            status_code=500,
            code="INTERNAL_ERROR",
            message="An unexpected server error occurred.",
            details={},
        )

    @app.get("/api/v1/health/live", tags=["health"])
    def liveness() -> dict[str, str]:
        """Confirm the process can serve HTTP; this does not check PostgreSQL."""
        return {"status": "alive"}

    @app.get("/api/v1/health/ready", tags=["health"], response_model=None)
    def readiness(request: Request) -> dict[str, Any] | JSONResponse:
        """Confirm PostgreSQL is reachable with a bounded, read-only query."""
        if request.app.state.database_configuration_error:
            return _error_response(
                request,
                status_code=503,
                code="DATABASE_CONFIGURATION_INVALID",
                message="The database connection configuration is invalid.",
                details={"database": "unavailable"},
            )
        if request.app.state.database_engine is None:
            return _error_response(
                request,
                status_code=503,
                code="DATABASE_CONFIGURATION_MISSING",
                message="The database connection is not configured.",
                details={"database": "unconfigured"},
            )
        try:
            request.app.state.database_probe(request.app.state.database_engine)
        except SQLAlchemyError as error:
            details = _database_failure_details(error)
            logger.warning(
                "PostgreSQL readiness probe failed: category=%s sqlstate=%s",
                details["failure_category"],
                details["sqlstate"] or "none",
            )
            return _error_response(
                request,
                status_code=503,
                code="DATABASE_UNAVAILABLE",
                message="PostgreSQL is not ready to accept connections.",
                details=details,
            )
        return {"status": "ready", "checks": {"database": "ready"}}

    return app


app = create_app()
