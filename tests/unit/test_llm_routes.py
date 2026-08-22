"""Endpoint tests for the public LLM catalogue routes.

These two routes require no authentication and no database — they back the
provider/model pickers in the settings UI. They are the only PR-3 surface
verifiable without an authenticated, seeded project.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from ontokit.api.routes.llm import (
    _config_to_response,
    _provider_connection_failure,
    get_llm_config,
    update_llm_config,
)
from ontokit.api.routes.llm import test_llm_connection as call_test_llm_connection
from ontokit.core.auth import ANONYMOUS_USER, CurrentUser
from ontokit.schemas.llm import LLMConfigUpdate, LLMProviderType


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
    resp = client.get("/api/v1/projects/00000000-0000-0000-0000-000000000000/llm/config")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_project_llm_route_rejects_disabled_auth_anonymous_identity() -> None:
    from fastapi import HTTPException

    session = AsyncMock()
    with pytest.raises(HTTPException) as raised:
        await get_llm_config(
            UUID("12345678-1234-5678-1234-567812345678"),
            session,
            ANONYMOUS_USER,
        )

    assert raised.value.status_code == 403
    assert "authenticated" in raised.value.detail.lower()
    session.execute.assert_not_awaited()


def _stored_config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "model_tier": "cheap",
        "api_key_encrypted": "encrypted-old-key",
        "base_url": "https://gateway.example.test/v1",
        "monthly_budget_usd": 10.0,
        "daily_cap_usd": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        LLMConfigUpdate(provider="anthropic"),
        LLMConfigUpdate(base_url="https://other.example.test/v1"),
    ],
)
async def test_config_scope_change_without_replacement_key_clears_secret(
    update: LLMConfigUpdate,
) -> None:
    config = _stored_config()
    db = AsyncMock()

    with (
        patch("ontokit.api.routes.llm._require_owner_or_admin", new=AsyncMock()),
        patch("ontokit.api.routes.llm._get_llm_config", new=AsyncMock(return_value=config)),
        patch("ontokit.api.routes.llm.validate_base_url"),
    ):
        await update_llm_config(
            UUID("12345678-1234-5678-1234-567812345678"),
            update,
            db,
            SimpleNamespace(id="owner", is_superadmin=False, is_anonymous=False),
        )

    assert config.api_key_encrypted is None


@pytest.mark.asyncio
async def test_config_path_change_on_same_canonical_origin_preserves_secret() -> None:
    config = _stored_config()
    db = AsyncMock()

    with (
        patch("ontokit.api.routes.llm._require_owner_or_admin", new=AsyncMock()),
        patch("ontokit.api.routes.llm._get_llm_config", new=AsyncMock(return_value=config)),
        patch("ontokit.api.routes.llm.validate_base_url"),
    ):
        await update_llm_config(
            UUID("12345678-1234-5678-1234-567812345678"),
            LLMConfigUpdate(base_url="https://gateway.example.test/v2"),
            db,
            SimpleNamespace(id="owner", is_superadmin=False, is_anonymous=False),
        )

    assert config.api_key_encrypted == "encrypted-old-key"


@pytest.mark.asyncio
async def test_config_scope_change_with_replacement_key_stores_only_new_secret() -> None:
    config = _stored_config()
    db = AsyncMock()

    with (
        patch("ontokit.api.routes.llm._require_owner_or_admin", new=AsyncMock()),
        patch("ontokit.api.routes.llm._get_llm_config", new=AsyncMock(return_value=config)),
        patch("ontokit.api.routes.llm.validate_base_url"),
        patch("ontokit.api.routes.llm.encrypt_secret", return_value="encrypted-new-key") as encrypt,
    ):
        await update_llm_config(
            UUID("12345678-1234-5678-1234-567812345678"),
            LLMConfigUpdate(provider="anthropic", api_key="new-key"),
            db,
            SimpleNamespace(id="owner", is_superadmin=False, is_anonymous=False),
        )

    assert config.api_key_encrypted == "encrypted-new-key"
    encrypt.assert_called_once_with("new-key")


def test_config_response_strips_legacy_url_userinfo() -> None:
    response = _config_to_response(
        _stored_config(base_url="https://legacy-user:legacy-pass@gateway.example.test/v1")
    )

    assert response.base_url == "https://gateway.example.test/v1"
    assert "legacy-user" not in response.model_dump_json()
    assert "legacy-pass" not in response.model_dump_json()


def test_provider_connection_failure_never_echoes_outbound_error(caplog):
    marker = "internal-host.example:8443 returned sk-sensitive"

    response = _provider_connection_failure("custom", RuntimeError(marker))

    assert response == {"success": False, "error": "Provider connection failed"}
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_connection_route_redacts_provider_exception(
    caplog, authenticated_user: CurrentUser
) -> None:
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
        patch(
            "ontokit.api.routes.llm.get_model_pricing",
            new=AsyncMock(return_value=(0.01, 0.02)),
        ),
        patch("ontokit.api.routes.llm.get_provider", return_value=provider),
        patch(
            "ontokit.api.routes.llm.reserve_llm_call",
            new=AsyncMock(return_value=(uuid4(), None)),
        ),
        patch(
            "ontokit.api.routes.llm.finalize_llm_call",
            new=AsyncMock(),
        ) as finalize,
    ):
        response = await call_test_llm_connection(
            UUID("12345678-1234-5678-1234-567812345678"),
            AsyncMock(),
            authenticated_user,
        )

    assert response == {"success": False, "error": "Provider connection failed"}
    assert marker not in caplog.text
    assert finalize.await_args.kwargs["succeeded"] is False


@pytest.mark.asyncio
async def test_connection_reserves_paid_call_before_provider(
    authenticated_user: CurrentUser,
) -> None:
    events: list[str] = []
    config = SimpleNamespace(
        provider="openai",
        base_url=None,
        api_key_encrypted=None,
        model="gpt-4o-mini",
        monthly_budget_usd=1.0,
        daily_cap_usd=None,
    )
    provider = MagicMock()

    async def reserve(*_args, **_kwargs):
        events.append("reserve")
        return uuid4(), None

    async def connect() -> bool:
        events.append("provider")
        return True

    async def finalize(*_args, **_kwargs) -> None:
        events.append("finalize")

    provider.test_connection = connect
    with (
        patch(
            "ontokit.api.routes.llm._require_owner_or_admin",
            new=AsyncMock(return_value="admin"),
        ),
        patch(
            "ontokit.api.routes.llm._get_llm_config",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "ontokit.api.routes.llm.get_model_pricing",
            new=AsyncMock(return_value=(0.01, 0.02)),
        ),
        patch("ontokit.api.routes.llm.get_provider", return_value=provider),
        patch(
            "ontokit.api.routes.llm.reserve_llm_call",
            new=AsyncMock(side_effect=reserve),
        ),
        patch(
            "ontokit.api.routes.llm.finalize_llm_call",
            new=AsyncMock(side_effect=finalize),
        ),
    ):
        response = await call_test_llm_connection(
            UUID("12345678-1234-5678-1234-567812345678"),
            AsyncMock(),
            authenticated_user,
        )

    assert response == {"success": True}
    assert events == ["reserve", "provider", "finalize"]


@pytest.mark.asyncio
async def test_connection_budget_refusal_prevents_provider(
    authenticated_user: CurrentUser,
) -> None:
    config = SimpleNamespace(
        provider="openai",
        base_url=None,
        api_key_encrypted=None,
        model="gpt-4o-mini",
        monthly_budget_usd=1.0,
        daily_cap_usd=None,
    )
    with (
        patch(
            "ontokit.api.routes.llm._require_owner_or_admin",
            new=AsyncMock(return_value="admin"),
        ),
        patch(
            "ontokit.api.routes.llm._get_llm_config",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "ontokit.api.routes.llm.get_model_pricing",
            new=AsyncMock(return_value=(0.01, 0.02)),
        ),
        patch(
            "ontokit.api.routes.llm.reserve_llm_call",
            new=AsyncMock(return_value=(None, "budget_exhausted")),
        ),
        patch("ontokit.api.routes.llm.get_provider") as provider_factory,
    ):
        response = await call_test_llm_connection(
            UUID("12345678-1234-5678-1234-567812345678"),
            AsyncMock(),
            authenticated_user,
        )

    assert response == {"success": False, "error": "Monthly LLM budget exhausted"}
    provider_factory.assert_not_called()
