"""Fail-closed proofs for route-local project authorization helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException

from ontokit.api.routes.generation import _require_project_member as require_generation_member
from ontokit.api.routes.llm import _require_project_member as require_llm_member
from ontokit.api.routes.llm import update_llm_config
from ontokit.api.routes.translation import _require_member as require_translation_member
from ontokit.api.routes.translation import update_translation_config
from ontokit.core.auth import CurrentUser
from ontokit.models.project import Project, ProjectMember

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
    statement = str(db.execute.await_args.args[0])
    assert "projects.is_demo" in statement
    assert "projects.is_public" in statement


@pytest.mark.parametrize("authorize", AUTHORIZERS)
async def test_hidden_demo_is_denied_before_superadmin_fallback(
    authorize: RouteAuthorizer,
) -> None:
    """Superadmin fallback cannot disclose a preparing or retired generation."""
    db = AsyncMock()
    db.execute.side_effect = [_result(None), _result(_demo_project(is_public=False))]

    with pytest.raises(HTTPException) as exc_info:
        await authorize(db, PROJECT_ID, "superadmin", True)

    assert exc_info.value.status_code == 404


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
