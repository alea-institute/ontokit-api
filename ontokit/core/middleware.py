"""Request middleware for OntoKit API."""

import logging
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ontokit.core.api_paths import (
    ANONYMOUS_BEACON_PATH_PATTERN,
    ANONYMOUS_SAVE_PATH_PATTERN,
)
from ontokit.core.limits import MAX_TURTLE_PAYLOAD_BYTES

logger = logging.getLogger(__name__)

class AnonymousSuggestionBodyLimitMiddleware:
    """Reject oversized anonymous save bodies before request parsing.

    ``Content-Length`` provides an early refusal for honest clients. The
    bounded pre-read is authoritative for chunked, missing, or spoofed lengths;
    the downstream application is not invoked until the complete body is known
    to fit.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = MAX_TURTLE_PAYLOAD_BYTES,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    @staticmethod
    def _is_limited_request(scope: Scope) -> bool:
        if scope["type"] != "http":
            return False
        method = scope.get("method", "")
        path = scope.get("path", "")
        return bool(
            (method == "PUT" and ANONYMOUS_SAVE_PATH_PATTERN.fullmatch(path))
            or (method == "POST" and ANONYMOUS_BEACON_PATH_PATTERN.fullmatch(path))
        )

    @staticmethod
    def _declared_length(scope: Scope) -> int | None:
        values = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if not values:
            return None
        try:
            lengths = {int(value) for value in values}
        except ValueError:
            return -1
        if len(lengths) != 1:
            return -1
        return lengths.pop()

    @staticmethod
    async def _respond(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse(status_code=status_code, content={"detail": detail})
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_limited_request(scope):
            await self.app(scope, receive, send)
            return

        declared = self._declared_length(scope)
        if declared is not None and declared < 0:
            await self._respond(scope, receive, send, 400, "Invalid Content-Length")
            return
        if declared is not None and declared > self.max_body_bytes:
            await self._respond(scope, receive, send, 413, "Request body too large")
            return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    await self._respond(
                        scope, receive, send, 413, "Request body too large"
                    )
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a unique request ID to every request and include it in the response.

    If the client sends an ``X-Request-ID`` header the value is reused;
    otherwise a new UUID4 is generated.  The ID is stored in
    ``request.state.request_id`` so downstream code can reference it and is
    returned in the ``X-Request-ID`` response header.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status code and duration."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()

        response = await call_next(request)

        duration_ms = (time.perf_counter() - start) * 1000
        request_id = getattr(request.state, "request_id", "-")

        logger.info(
            "%s %s %d %.1fms [%s]",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )

        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to every response."""

    def __init__(self, app: Any, *, is_production: bool = False) -> None:
        super().__init__(app)
        self.is_production = is_production

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"

        if self.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )

        return response
