"""Custom exception types and global exception handlers for OntoKit API."""

from typing import Any

from fastapi import HTTPException


class GuidedHTTPException(HTTPException):
    """An HTTP refusal with curated guidance alongside its unchanged detail."""

    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        code: str,
        message: str,
        recovery: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.message = message
        self.recovery = recovery


class OntoKitError(Exception):
    """Base exception for all OntoKit domain errors."""

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFoundError(OntoKitError):
    """Raised when a requested resource does not exist."""

    def __init__(self, resource: str = "Resource", *, detail: Any = None) -> None:
        super().__init__(f"{resource} not found", detail=detail)
        self.resource = resource


class ValidationError(OntoKitError):
    """Raised when input validation fails at the domain level."""


class ConflictError(OntoKitError):
    """Raised when an operation conflicts with existing state."""


class ForbiddenError(OntoKitError):
    """Raised when the user lacks permission for the operation."""
