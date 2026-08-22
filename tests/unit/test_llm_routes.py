"""Endpoint tests for the public LLM catalogue routes.

These two routes require no authentication and no database — they back the
provider/model pickers in the settings UI. They are the only PR-3 surface
verifiable without an authenticated, seeded project.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from ontokit.api.routes.llm import _provider_connection_failure, test_llm_connection
from ontokit.schemas.llm import LLMProviderType


def test_list_providers_public(client: TestClient):
    """GET /api/v1/llm/providers returns all providers, no auth required."""
    resp = client.get("/api/v1/llm/providers")
    assert resp.status_code == 200

    providers = resp.json()
    returned = {p["provider"] for p in providers}
    assert returned == {p.value for p in LLMProviderType}

    for p in providers:
        assert set(p) == {"provider", "display_name", "requires_api_key", "icon_name"}
        assert isinstance(p["requires_api_key"], bool)
        # No secret material must appear in the public catalogue.
        assert "api_key" not in p
        assert "key" not in {k.lower() for k in p if k != "requires_api_key"}


def test_list_known_models_public(client: TestClient):
    """GET /api/v1/llm/known-models returns model metadata, no auth required."""
    resp = client.get("/api/v1/llm/known-models")
    assert resp.status_code == 200

    models = resp.json()
    assert len(models) > 0
    valid_providers = {p.value for p in LLMProviderType}
    for m in models:
        assert set(m) == {"provider", "model_id", "display_name", "tier"}
        assert m["provider"] in valid_providers
        assert m["tier"] in ("quality", "cheap")


def test_project_llm_config_requires_auth(client: TestClient):
    """Project-scoped LLM config must NOT be reachable without authentication."""
    resp = client.get(
        "/api/v1/projects/00000000-0000-0000-0000-000000000000/llm/config"
    )
    assert resp.status_code in (401, 403)


def test_provider_connection_failure_never_echoes_outbound_error(caplog):
    marker = "internal-host.example:8443 returned sk-sensitive"

    response = _provider_connection_failure("custom", RuntimeError(marker))

    assert response == {"success": False, "error": "Provider connection failed"}
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_connection_route_redacts_provider_exception(caplog) -> None:
    marker = "https://internal-host.example:8443 sk-sensitive upstream body"
    config = SimpleNamespace(
        provider="custom",
        base_url=None,
        api_key_encrypted=None,
        model="gateway-model",
    )
    provider = MagicMock()
    provider.test_connection = AsyncMock(side_effect=RuntimeError(marker))

    with (
        patch(
            "ontokit.api.routes.llm._require_owner_or_admin",
            new=AsyncMock(return_value="admin"),
        ),
        patch(
            "ontokit.api.routes.llm._get_llm_config",
            new=AsyncMock(return_value=config),
        ),
        patch("ontokit.api.routes.llm.get_provider", return_value=provider),
    ):
        response = await test_llm_connection(
            UUID("12345678-1234-5678-1234-567812345678"),
            AsyncMock(),
            SimpleNamespace(id="user-1", is_superadmin=False),
        )

    assert response == {"success": False, "error": "Provider connection failed"}
    assert marker not in caplog.text
