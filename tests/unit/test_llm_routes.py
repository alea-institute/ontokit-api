"""Endpoint tests for the public LLM catalogue routes.

These two routes require no authentication and no database — they back the
provider/model pickers in the settings UI. They are the only PR-3 surface
verifiable without an authenticated, seeded project.
"""

from fastapi.testclient import TestClient

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
