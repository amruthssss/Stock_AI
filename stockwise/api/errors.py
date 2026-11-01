"""Application-level exceptions with safe API error responses."""


class DatabaseUnavailableError(RuntimeError):
    """Raised when a database-backed API route has no configured engine."""
