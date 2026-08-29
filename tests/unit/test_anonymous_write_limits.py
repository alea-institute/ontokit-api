"""Resource-boundary tests for anonymous full-document saves."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ontokit.core.api_paths import _compile_route_path
from ontokit.core.limits import MAX_ANONYMOUS_REQUEST_BYTES, MAX_TURTLE_PAYLOAD_BYTES
from ontokit.core.middleware import AnonymousSuggestionBodyLimitMiddleware
from ontokit.models.suggestion_session import SuggestionSession
from ontokit.schemas.suggestion import SuggestionBeaconRequest, SuggestionSaveRequest

SAVE_PATH = "/api/v1/projects/p/suggestions/anonymous/sessions/s/save"
BEACON_PATH = "/api/v1/projects/p/suggestions/anonymous/beacon"


def _limited_client(limit: int, touched: list[str]) -> TestClient:
    probe = FastAPI()

    @probe.api_route(SAVE_PATH, methods=["PUT"])
    @probe.api_route(BEACON_PATH, methods=["POST"])
    async def sink(request: Request) -> JSONResponse:
        touched.append(request.url.path)
        await request.body()
        return JSONResponse({"accepted": True})

    probe.add_middleware(AnonymousSuggestionBodyLimitMiddleware, max_body_bytes=limit)
    return TestClient(probe, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("method", "path"),
    [("put", SAVE_PATH), ("post", BEACON_PATH)],
)
@pytest.mark.parametrize(
    ("headers", "body_factory"),
    [
        ({"content-length": "1000"}, lambda: b"{}"),
        ({"content-length": "2"}, lambda: b"x" * 33),
        ({"transfer-encoding": "chunked"}, lambda: b"x" * 33),
    ],
    ids=["declared-too-large", "spoofed-too-small", "missing-content-length"],
)
def test_anonymous_save_bodies_are_rejected_before_parsing(
    method: str,
    path: str,
    headers: dict[str, str],
    body_factory: Callable[[], bytes],
) -> None:
    touched: list[str] = []
    client = _limited_client(32, touched)

    response = getattr(client, method)(path, content=body_factory(), headers=headers)

    assert response.status_code == 413
    assert touched == []


@pytest.mark.parametrize(
    ("method", "path"),
    [("put", SAVE_PATH), ("post", BEACON_PATH)],
)
def test_anonymous_save_bodies_at_the_limit_reach_the_route(
    method: str,
    path: str,
) -> None:
    touched: list[str] = []
    client = _limited_client(32, touched)

    response = getattr(client, method)(path, content=b"x" * 32)

    assert response.status_code == 200
    assert touched == [path]


def test_invalid_content_length_is_rejected_before_parsing() -> None:
    touched: list[str] = []
    client = _limited_client(32, touched)

    response = client.put(
        SAVE_PATH,
        content=b"{}",
        headers={"content-length": "not-a-number"},
    )

    assert response.status_code == 400
    assert touched == []


def test_maximum_ascii_turtle_document_fits_in_json_request_budget() -> None:
    request = SuggestionSaveRequest(
        content="x" * MAX_TURTLE_PAYLOAD_BYTES,
        entity_iri="https://example.test/entity",
        entity_label="Entity",
    )

    assert len(request.model_dump_json().encode()) <= MAX_ANONYMOUS_REQUEST_BYTES


@pytest.mark.parametrize("schema", [SuggestionSaveRequest, SuggestionBeaconRequest])
def test_turtle_content_has_a_schema_level_ceiling(schema: type[object]) -> None:
    content_schema = schema.model_json_schema()["properties"]["content"]  # type: ignore[attr-defined]

    assert content_schema["maxLength"] == MAX_TURTLE_PAYLOAD_BYTES


def test_anonymous_byte_counter_is_non_null_and_zero_defaulted() -> None:
    column = SuggestionSession.__table__.c.anonymous_content_bytes

    assert column.nullable is False
    assert column.server_default is not None
    assert str(column.server_default.arg) == "0"


def test_route_pattern_derives_every_placeholder_from_its_template() -> None:
    pattern = _compile_route_path("/{project_id}/suggestions/{future_id}/save")

    assert pattern.fullmatch("/api/v1/projects/p/suggestions/f/save")


def test_stale_anonymous_reaper_has_a_matching_partial_index() -> None:
    index = next(
        item
        for item in SuggestionSession.__table__.indexes
        if item.name == "ix_suggestion_sessions_stale_anonymous"
    )

    assert tuple(column.name for column in index.columns) == ("last_activity", "id")
    assert str(index.dialect_options["postgresql"]["where"]) == (
        "status = 'active' AND is_anonymous IS true"
    )
