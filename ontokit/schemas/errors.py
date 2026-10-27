"""Optional guidance for explicitly classified HTTP refusals."""

from typing import Any

from pydantic import BaseModel, Field


class ErrorGuidance(BaseModel):
    """Safe next steps, correlated with the existing X-Request-ID header."""

    code: str
    message: str
    recovery: str
    request_id: str


class HTTPErrorResponse(BaseModel):
    """Legacy HTTP detail, optionally accompanied by classified guidance."""

    detail: Any
    error: ErrorGuidance | None = Field(
        default=None,
        description="Present only for explicitly classified refusals; retain the detail fallback.",
    )
