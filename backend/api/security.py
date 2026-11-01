"""Authentication dependencies for local-only administrative API actions."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request


MINIMUM_ADMIN_TOKEN_LENGTH = 32


def authorization_matches(authorization: str | None, expected_token: str) -> bool:
    """Check a Bearer token without comparing secrets with ordinary string equality."""
    if not authorization:
        return False
    scheme, separator, supplied_token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied_token:
        return False
    return hmac.compare_digest(supplied_token, expected_token)


def require_approval_admin(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    """Require a configured strong local admin token for each approval write."""
    expected_token = request.app.state.settings.approval_admin_token
    if not expected_token or len(expected_token) < MINIMUM_ADMIN_TOKEN_LENGTH:
        raise HTTPException(status_code=503, detail="Approval authentication is not configured")
    if not authorization_matches(authorization, expected_token):
        raise HTTPException(
            status_code=401,
            detail="A valid approval admin token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "local_admin"
