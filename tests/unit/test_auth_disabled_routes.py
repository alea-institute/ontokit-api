"""Disabled auth write protections and explicit exceptions on the real FastAPI app."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import routing
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from ontokit.api.routes.anonymous_suggestions import get_service as get_suggestion_service
from ontokit.api.routes.normalization import get_service as get_normalization_project_service
from ontokit.api.routes.projects import get_service as get_project_service
from ontokit.core.auth import ANONYMOUS_USER, get_current_user, get_current_user_with_token
from ontokit.main import app

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
DETAIL = (
    "Authentication is disabled on this deployment, so it is read-only "
    "apart from anonymous suggestions"
)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Every exception is an exact mounted method/path pair with its reason.
DISABLED_WRITE_ALLOWLIST = {
    ("POST", "/api/v1/search/sparql"): "Read-only query; SPARQL UPDATE is rejected",
    (
        "POST",
        "/api/v1/projects/{project_id}/suggestions/anonymous/sessions",
    ): "Anonymous proposal session creation is the suggest in read-and-suggest",
    (
        "PUT",
        "/api/v1/projects/{project_id}/suggestions/anonymous/sessions/{session_id}/save",
    ): "Anonymous proposal saving is the suggest in read-and-suggest",
    (
        "POST",
        "/api/v1/projects/{project_id}/suggestions/anonymous/sessions/{session_id}/submit",
    ): "Anonymous proposal submission is the suggest in read-and-suggest",
    (
        "POST",
        "/api/v1/projects/{project_id}/suggestions/anonymous/sessions/{session_id}/discard",
    ): "Anonymous proposal discard is the suggest in read-and-suggest",
    (
        "POST",
        "/api/v1/projects/{project_id}/suggestions/anonymous/beacon",
    ): "Token-authenticated anonymous proposal beacon",
    (
        "POST",
        "/api/v1/projects/{project_id}/suggestions/beacon",
    ): "Token-authenticated suggestion beacon",
    (
        "POST",
        "/api/v1/projects/webhooks/github/{project_id}",
    ): "Signature-authenticated GitHub webhook",
    ("POST", "/api/v1/auth/device/code"): "Auth endpoint initiates device authorization",
    ("POST", "/api/v1/auth/device/token"): "Auth endpoint exchanges device code for tokens",
    ("POST", "/api/v1/auth/token/refresh"): "Auth endpoint refreshes tokens",
}


def _requires_user(dependency: Dependant) -> bool:
    return dependency.call in (get_current_user, get_current_user_with_token) or any(
        _requires_user(child) for child in dependency.dependencies
    )


def _all_write_routes() -> list[tuple[Any, str]]:
    # Newer FastAPI versions keep included routers lazy; their public iterator
    # resolves the same route/dependency tree with the full mounted path.
    iterator = getattr(routing, "iter_route_contexts", iter)
    cases = [
        (route, method)
        for route in iterator(app.routes)
        if isinstance(getattr(route, "original_route", route), APIRoute)
        for method in sorted(route.methods - SAFE_METHODS)
    ]
    assert cases, "The write inventory must not silently become empty"
    return cases


def _write_routes() -> list[tuple[Any, str]]:
    cases = [
        (route, method) for route, method in _all_write_routes() if _requires_user(route.dependant)
    ]
    assert cases, "The protected-write inventory must not silently become empty"
    return cases


def test_disabled_mode_write_inventory_is_exhaustive() -> None:
    routes = _all_write_routes()
    mounted = {(method, route.path) for route, method in routes}
    assert DISABLED_WRITE_ALLOWLIST.keys() <= mounted, "Remove unmounted allowlist entries"
    # OptionalUser refresh has an explicit handler gate, covered behaviorally below.
    explicitly_gated = ("POST", "/api/v1/projects/{project_id}/normalization/refresh")
    assert explicitly_gated in mounted
    uncovered = {
        (method, route.path)
        for route, method in routes
        if not _requires_user(route.dependant)
        and (method, route.path) != explicitly_gated
        and (method, route.path) not in DISABLED_WRITE_ALLOWLIST
    }
    assert not uncovered, (
        f"Writes without a disabled-mode gate or documented exception: {uncovered}"
    )


def test_disabled_mode_write_inventory_rejects_unmounted_allowlist_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(DISABLED_WRITE_ALLOWLIST, ("POST", "/api/v1/unmounted"), "Stale exception")
    with pytest.raises(AssertionError, match="Remove unmounted allowlist entries"):
        test_disabled_mode_write_inventory_is_exhaustive()


@pytest.mark.parametrize("auth_mode", ["required", "optional", "disabled"])
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/ontologies"),
        ("POST", f"/api/v1/ontologies/{PROJECT_ID}/classes"),
        ("PUT", f"/api/v1/ontologies/{PROJECT_ID}/properties/x"),
    ],
)
async def test_legacy_routes_are_absent(
    monkeypatch: pytest.MonkeyPatch, auth_mode: str, method: str, path: str
) -> None:
    monkeypatch.setattr("ontokit.core.auth.settings.auth_mode", auth_mode)
    monkeypatch.setattr(app, "dependency_overrides", {})
    service = MagicMock()
    # If a legacy route is reintroduced, prevent its dependencies from touching
    # infrastructure while still exercising routing and request validation.
    for route, _ in _all_write_routes():
        if route.path.startswith("/api/v1/ontologies"):
            _stub_unrelated_dependencies(route.dependant, monkeypatch, service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert service.mock_calls == []


@pytest.fixture
async def disabled_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr("ontokit.core.auth.settings.auth_mode", "disabled")
    # Keep the actual auth dependency tree. Stub unrelated dependencies before
    # auth so requests cannot access infrastructure or construct real services.
    monkeypatch.setattr(app, "dependency_overrides", {})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.parametrize("auth_mode", ["disabled", "optional"])
async def test_normalization_refresh_disabled_mode_gate(
    disabled_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, auth_mode: str
) -> None:
    monkeypatch.setattr("ontokit.core.auth.settings.auth_mode", auth_mode)
    service = MagicMock()
    service.get = AsyncMock()
    pool = AsyncMock()
    pool.enqueue_job.return_value = MagicMock(job_id="refresh-job")
    pool_factory = AsyncMock(return_value=pool)
    monkeypatch.setattr("ontokit.api.routes.normalization.get_arq_pool", pool_factory)

    async def stub() -> MagicMock:
        return service

    monkeypatch.setitem(app.dependency_overrides, get_normalization_project_service, stub)

    response = await disabled_client.post(f"/api/v1/projects/{PROJECT_ID}/normalization/refresh")

    if auth_mode == "disabled":
        assert response.status_code == 403
        assert response.json() == {"detail": DETAIL}
        assert service.mock_calls == []
        pool_factory.assert_not_called()
        assert pool.mock_calls == []
    else:
        assert response.status_code == 200
        assert response.json()["job_id"] == "refresh-job"
        service.get.assert_awaited_once_with(UUID(PROJECT_ID), None)
        pool_factory.assert_awaited_once_with()
        pool.enqueue_job.assert_awaited_once_with("check_normalization_status_task", PROJECT_ID)


def _stub_unrelated_dependencies(
    dependency: Dependant, monkeypatch: pytest.MonkeyPatch, service: MagicMock
) -> None:
    async def stub() -> MagicMock:
        return service

    for child in dependency.dependencies:
        if child.call in (get_current_user, get_current_user_with_token):
            continue
        if _requires_user(child):
            _stub_unrelated_dependencies(child, monkeypatch, service)
        else:
            monkeypatch.setitem(app.dependency_overrides, child.call, stub)


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/projects"),
        ("POST", "/api/v1/projects/import"),
        ("PUT", f"/api/v1/projects/{PROJECT_ID}/source"),
        ("POST", f"/api/v1/projects/{PROJECT_ID}/branches"),
        ("POST", f"/api/v1/projects/{PROJECT_ID}/suggestions/sessions/s_test/reopen"),
    ],
)
async def test_project_writes_refused_before_validation_or_service(
    disabled_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    service = MagicMock()
    for route, _ in _write_routes():
        _stub_unrelated_dependencies(route.dependant, monkeypatch, service)
    # Missing required body/form fields must still produce 403, not 422.
    response = await disabled_client.request(method, path)
    assert response.status_code == 403
    assert response.json() == {"detail": DETAIL}
    assert service.mock_calls == []


@pytest.mark.parametrize(
    "route,method",
    _write_routes(),
    ids=[f"{method} {route.path}" for route, method in _write_routes()],
)
async def test_every_required_user_write_is_read_only(
    disabled_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    route: APIRoute,
    method: str,
) -> None:
    """New protected write routes automatically join this behavioral inventory."""
    service = MagicMock()
    # Some mounted routers share paths; stub infrastructure on all candidates.
    for candidate, _ in _write_routes():
        _stub_unrelated_dependencies(candidate.dependant, monkeypatch, service)
    path = route.path_format.format(**dict.fromkeys(route.param_convertors, PROJECT_ID))
    response = await disabled_client.request(method, path)
    assert response.status_code == 403
    assert response.json() == {"detail": DETAIL}
    assert service.mock_calls == []


async def test_project_list_remains_available(
    disabled_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock()
    service.list_accessible = AsyncMock(
        return_value={"items": [], "total": 0, "unfiltered_total": 0, "skip": 0, "limit": 20}
    )

    async def stub() -> MagicMock:
        return service

    monkeypatch.setitem(app.dependency_overrides, get_project_service, stub)
    response = await disabled_client.get("/api/v1/projects")
    assert response.status_code == 200
    assert response.json()["items"] == []
    service.list_accessible.assert_awaited_once()
    assert service.list_accessible.await_args.args[0] is ANONYMOUS_USER


async def test_anonymous_suggestion_session_remains_available(
    disabled_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock()
    service.create_anonymous_session = AsyncMock(
        return_value={
            "session_id": "s_test",
            "branch": "suggestion/test",
            "created_at": datetime.now(UTC),
            "anonymous_token": "test-token",
        }
    )

    async def stub() -> MagicMock:
        return service

    monkeypatch.setitem(app.dependency_overrides, get_suggestion_service, stub)
    response = await disabled_client.post(
        f"/api/v1/projects/{PROJECT_ID}/suggestions/anonymous/sessions"
    )
    assert response.status_code == 201
    assert response.json()["session_id"] == "s_test"
    service.create_anonymous_session.assert_awaited_once_with(UUID(PROJECT_ID), "127.0.0.1")


async def test_duplicate_check_still_rejects_anonymous_at_sensitive_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing identity guard also protects direct handler invocations."""
    from fastapi import HTTPException

    from ontokit.api.routes.duplicate_check import check_duplicate
    from ontokit.schemas.duplicate_check import DuplicateCheckRequest

    service_factory = MagicMock()
    monkeypatch.setattr("ontokit.api.routes.duplicate_check.get_project_service", service_factory)
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await check_duplicate(
            project_id=UUID(PROJECT_ID),
            request=DuplicateCheckRequest(label="Example", entity_type="class"),
            db=db,
            user=ANONYMOUS_USER,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "An authenticated identity is required for this feature"
    service_factory.assert_not_called()
    assert db.mock_calls == []


@pytest.mark.parametrize("anonymous", [False, True])
async def test_suggestion_beacon_remains_available(
    disabled_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, anonymous: bool
) -> None:
    from ontokit.api.routes.suggestions import get_service as get_authenticated_suggestion_service
    from ontokit.core.anonymous_token import create_anonymous_token

    monkeypatch.setattr(
        "ontokit.core.anonymous_token.settings.secret_key", "test-secret-long-enough"
    )
    service = MagicMock()
    service.beacon_save = AsyncMock(return_value=None)
    service.beacon_save_anonymous = AsyncMock(return_value=None)

    async def stub() -> MagicMock:
        return service

    dependency = get_suggestion_service if anonymous else get_authenticated_suggestion_service
    monkeypatch.setitem(app.dependency_overrides, dependency, stub)
    token = create_anonymous_token("s_test") if anonymous else "service-verified-token"
    prefix = "anonymous/" if anonymous else ""
    response = await disabled_client.post(
        f"/api/v1/projects/{PROJECT_ID}/suggestions/{prefix}beacon",
        params={"token": token},
        json={"session_id": "s_test", "content": "example"},
    )
    assert response.status_code == 204
    if anonymous:
        service.beacon_save_anonymous.assert_awaited_once()
        assert service.beacon_save_anonymous.await_args.args[2] == "s_test"
    else:
        service.beacon_save.assert_awaited_once()
        assert service.beacon_save.await_args.args[2] == token


async def test_pr_sync_still_rejects_anonymous_at_sensitive_boundary() -> None:
    from fastapi import HTTPException

    from ontokit.api.routes.pull_requests import retry_pull_request_github_sync

    service = MagicMock()
    with pytest.raises(HTTPException) as exc:
        await retry_pull_request_github_sync(
            project_id=UUID(PROJECT_ID), pr_number=1, service=service, user=ANONYMOUS_USER
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "An authenticated identity is required for this feature"
    assert service.mock_calls == []


def test_reopen_is_in_protected_write_inventory() -> None:
    assert ("POST", "/api/v1/projects/{project_id}/suggestions/sessions/{session_id}/reopen") in {
        (method, route.path) for route, method in _write_routes()
    }
