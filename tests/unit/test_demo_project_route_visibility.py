"""Fail-closed proofs for route-local project authorization helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from ontokit.api.routes.generation import _require_project_member as require_generation_member
from ontokit.api.routes.llm import _require_project_member as require_llm_member
from ontokit.api.routes.llm import update_llm_config
from ontokit.api.routes.projects import get_service
from ontokit.api.routes.translation import _require_member as require_translation_member
from ontokit.api.routes.translation import update_translation_config
from ontokit.core.auth import CurrentUser, get_current_user_optional
from ontokit.main import app
from ontokit.models.demo_generation import DemoGeneration
from ontokit.models.project import Project, ProjectMember
from ontokit.services.project_access_policy import require_visible_project
from ontokit.services.project_service import ProjectService

PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")
OWNER_ID = "demo-owner"

RouteAuthorizer = Callable[[AsyncMock, UUID, str, bool], Awaitable[str]]
AUTHORIZERS: tuple[RouteAuthorizer, ...] = (
    require_generation_member,
    require_llm_member,
    require_translation_member,
)


def _result(value: object | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _demo_project(*, is_public: bool) -> Project:
    return Project(
        id=PROJECT_ID,
        name="Managed demo",
        owner_id=OWNER_ID,
        is_demo=True,
        is_public=is_public,
    )


@pytest.fixture
async def project_client() -> AsyncIterator[tuple[AsyncClient, AsyncMock]]:
    db = AsyncMock()
    service = ProjectService(db, git_service=MagicMock())

    async def override_service() -> ProjectService:
        return service

    async def override_user() -> None:
        return None

    app.dependency_overrides[get_service] = override_service
    app.dependency_overrides[get_current_user_optional] = override_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, db
    finally:
        app.dependency_overrides.pop(get_service)
        app.dependency_overrides.pop(get_current_user_optional)


async def test_retired_demo_single_project_get_as_anonymous(
    project_client: tuple[AsyncClient, AsyncMock],
) -> None:
    client, db = project_client
    retired_at = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    retired = _demo_project(is_public=False)
    retired.demo_source_project_id = uuid4()
    retired.demo_generation = DemoGeneration(status="retired", retired_at=retired_at)
    current = Project(
        id=uuid4(),
        is_demo=True,
        is_public=True,
        demo_source_project_id=retired.demo_source_project_id,
        demo_generation=DemoGeneration(status="active"),
    )
    db.execute.side_effect = [_result(retired), _result(current)]
    response = await client.get(f"/api/v1/projects/{PROJECT_ID}")

    assert response.status_code == 410
    assert response.json() == {
        "detail": {
            "code": "demo_generation_retired",
            "current_project_id": str(current.id),
            "retired_at": "2026-09-07T12:00:00Z",
        }
    }
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("generation_status", ["preparing", "failed", "retired"])
async def test_hidden_demo_single_project_get_has_no_pointer(
    project_client: tuple[AsyncClient, AsyncMock], generation_status: str
) -> None:
    client, db = project_client
    project = _demo_project(is_public=False)
    project.demo_source_project_id = uuid4()
    project.demo_generation = DemoGeneration(status=generation_status)
    db.execute.side_effect = [_result(project), _result(None)]

    response = await client.get(f"/api/v1/projects/{PROJECT_ID}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}
    assert db.execute.await_count == (2 if generation_status == "retired" else 1)


def test_single_project_openapi_documents_retirement() -> None:
    schema = app.openapi()
    response = schema["paths"]["/api/v1/projects/{project_id}"]["get"]["responses"]["410"]
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DemoGenerationRetiredResponse"
    }
    envelope = schema["components"]["schemas"]["DemoGenerationRetiredResponse"]
    assert envelope["properties"]["detail"] == {
        "$ref": "#/components/schemas/DemoGenerationRetiredDetail"
    }
    assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"


def test_retired_demo_denied_by_shared_visibility_policy() -> None:
    project = _demo_project(is_public=False)
    project.demo_generation = DemoGeneration(status="retired")

    with pytest.raises(HTTPException) as exc_info:
        require_visible_project(project)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Project not found"


@pytest.mark.parametrize("authorize", AUTHORIZERS)
async def test_hidden_demo_member_is_denied_by_route_local_authorizers(
    authorize: RouteAuthorizer,
) -> None:
    """A retained owner row cannot authorize a hidden generation by direct ID."""
    db = AsyncMock()
    # The membership statement's visibility join filters the retained row.
    db.execute.return_value = _result(None)

    with pytest.raises(HTTPException) as exc_info:
        await authorize(db, PROJECT_ID, OWNER_ID, False)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == (
        "Not a project member"
        if authorize is require_translation_member
        else "Not a member of this project"
    )
    statement = str(db.execute.await_args.args[0])
    assert "projects.is_demo" in statement
    assert "projects.is_public" in statement


@pytest.mark.parametrize("authorize", AUTHORIZERS)
@pytest.mark.parametrize("generation_status", ["preparing", "failed", "retired"])
async def test_hidden_demo_is_denied_before_superadmin_fallback(
    authorize: RouteAuthorizer,
    generation_status: str,
) -> None:
    """Superadmin fallback cannot disclose a preparing or retired generation."""
    db = AsyncMock()
    project = _demo_project(is_public=False)
    project.demo_generation = DemoGeneration(status=generation_status)
    db.execute.side_effect = [_result(None), _result(project)]

    with pytest.raises(HTTPException) as exc_info:
        await authorize(db, PROJECT_ID, "superadmin", True)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Project not found"


@pytest.mark.parametrize("authorize", AUTHORIZERS)
async def test_active_demo_remains_readable_to_retained_owner(
    authorize: RouteAuthorizer,
) -> None:
    """The public active generation still supports ordinary read authorization."""
    db = AsyncMock()
    db.execute.return_value = _result(
        ProjectMember(project_id=PROJECT_ID, user_id=OWNER_ID, role="owner")
    )

    assert await authorize(db, PROJECT_ID, OWNER_ID, False) == "owner"


@pytest.mark.parametrize(
    ("mutation", "payload"),
    (
        (update_llm_config, MagicMock()),
        (update_translation_config, MagicMock()),
    ),
)
async def test_active_demo_configuration_mutation_is_denied(
    mutation: Callable[..., Awaitable[object]], payload: object
) -> None:
    """An owner cannot independently change one active demo's configuration."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(ProjectMember(project_id=PROJECT_ID, user_id=OWNER_ID, role="owner")),
        _result(_demo_project(is_public=True)),
    ]

    with pytest.raises(HTTPException) as exc_info:
        await mutation(
            PROJECT_ID,
            payload,
            db,
            CurrentUser(id=OWNER_ID),
        )

    assert exc_info.value.status_code == 403
    db.commit.assert_not_awaited()
