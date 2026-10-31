"""Opt-in guidance travels through the real application middleware."""

from typing import Annotated
from uuid import UUID

import pytest
from fastapi import HTTPException, Query, Response
from httpx import ASGITransport, AsyncClient

from ontokit.core.config import settings
from ontokit.core.exceptions import GuidedHTTPException, NotFoundError
from ontokit.main import app
from ontokit.schemas.errors import HTTPErrorResponse


@pytest.fixture
def guidance_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # Restore the shared application's route table after each test.
    monkeypatch.setattr(app.router, "routes", list(app.router.routes))

    @app.get("/_test/guidance/{kind}")
    async def endpoint(kind: str, count: Annotated[int, Query(ge=1)] = 1) -> Response:
        if kind == "plain":
            raise HTTPException(400, "Existing refusal")
        if kind == "domain":
            raise NotFoundError("Project")
        if kind == "success":
            return Response(status_code=204)
        detail = (
            {"reason": "limited", "code": "LIMITED", "counts": {"used": count, "limit": 3}}
            if kind == "nested"
            else "Existing refusal"
        )
        raise GuidedHTTPException(
            status_code=429,
            detail=detail,
            code="test_refusal",
            message="This action is unavailable.",
            recovery="Check the current state before trying again.",
            headers={"Retry-After": "60", "WWW-Authenticate": "Bearer"},
        )


@pytest.mark.usefixtures("guidance_route")
@pytest.mark.parametrize("kind", ["string", "nested"])
@pytest.mark.parametrize("request_id", [None, "supplied-correlation"])
async def test_classified_error_preserves_detail_headers_and_correlation(
    kind: str, request_id: str | None
) -> None:
    origin = settings.cors_origins[0]
    headers = {"Origin": origin}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/_test/guidance/{kind}", headers=headers)

    correlation = response.headers["x-request-id"]
    if request_id is None:
        UUID(correlation)
    else:
        assert correlation == request_id
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["access-control-allow-origin"] == origin
    assert response.json() == {
        "detail": (
            {"reason": "limited", "code": "LIMITED", "counts": {"used": 1, "limit": 3}}
            if kind == "nested"
            else "Existing refusal"
        ),
        "error": {
            "code": "test_refusal",
            "message": "This action is unavailable.",
            "recovery": "Check the current state before trying again.",
            "request_id": correlation,
        },
    }


@pytest.mark.usefixtures("guidance_route")
@pytest.mark.parametrize(
    ("kind", "status", "body"),
    [
        ("plain", 400, {"detail": "Existing refusal"}),
        ("domain", 404, {"error": {"code": "not_found", "message": "Project not found"}}),
        ("success", 204, None),
    ],
)
async def test_unclassified_and_success_responses_unchanged(
    kind: str, status: int, body: object
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/_test/guidance/{kind}")
    assert response.status_code == status
    if body is None:
        assert response.content == b""
    else:
        assert response.json() == body


@pytest.mark.usefixtures("guidance_route")
async def test_request_validation_keeps_standard_detail_array() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/_test/guidance/string", params={"count": "0"})
    assert response.status_code == 422
    assert list(response.json()) == ["detail"]
    assert response.json()["detail"][0]["loc"] == ["query", "count"]


def test_guidance_is_optional_in_error_schema() -> None:
    schema = HTTPErrorResponse.model_json_schema()
    assert "error" in schema["properties"]
    assert "error" not in schema["required"]
    assert HTTPErrorResponse(detail="Legacy refusal").error is None


def test_guided_exception_stays_in_http_exception_family() -> None:
    detail = {"reason": "limited"}
    error = GuidedHTTPException(
        status_code=503, detail=detail, code="limited", message="Unavailable", recovery="Try later"
    )
    assert isinstance(error, HTTPException)
    assert error.detail is detail
    assert error.status_code == 503
