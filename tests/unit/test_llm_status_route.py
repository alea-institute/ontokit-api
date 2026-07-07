"""Endpoint tests for GET /llm/status and PATCH /members/{id}/flags (PR-4).

/llm/status is member-readable and advisory (the dispatch path re-checks
server-side). The member-flags PATCH is the privilege-granting endpoint
(ROLE-03) — its owner/admin gate is the security boundary tested here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

PROJECT_ID = "11111111-1111-1111-1111-111111111111"


def _scalar_one_or_none(value: object) -> Mock:
    result = Mock()
    result.scalar_one_or_none = Mock(return_value=value)
    return result


def _scalar_one(value: object) -> Mock:
    result = Mock()
    result.scalar_one = Mock(return_value=value)
    return result


def _member(role: str) -> Mock:
    member = Mock()
    member.role = role
    return member


def _llm_config(
    provider: str = "anthropic",
    api_key_encrypted: bytes | None = b"enc",
    monthly: float | None = 100.0,
    daily: float | None = None,
) -> Mock:
    config = Mock()
    config.provider = provider
    config.api_key_encrypted = api_key_encrypted
    config.monthly_budget_usd = monthly
    config.daily_cap_usd = daily
    config.model = None
    config.model_tier = "quality"
    config.base_url = None
    return config


# ── GET /llm/status ───────────────────────────────────────────────────────────


def test_status_requires_auth(client: TestClient):
    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/llm/status")
    assert resp.status_code in (401, 403)


def test_status_403_for_non_member(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(side_effect=[_scalar_one_or_none(None)])

    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/llm/status")
    assert resp.status_code == 403


def test_status_unconfigured_project(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_member("editor")),  # membership
            _scalar_one_or_none(None),  # no LLM config
        ]
    )

    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/llm/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["provider"] is None
    assert body["budget_exhausted"] is False
    assert body["daily_remaining"] is None
    assert body["monthly_spent_usd"] == 0.0


def test_status_editor_gets_static_daily_cap(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_member("editor")),
            _scalar_one_or_none(_llm_config(monthly=100.0)),
            _scalar_one(20.0),  # monthly spend
            _scalar_one(1.0),  # daily spend
            _scalar_one(7.0),  # 7d burn basis
        ]
    )

    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/llm/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["provider"] == "anthropic"
    assert body["budget_exhausted"] is False
    assert body["daily_remaining"] == 500  # COST-03 static cap (Redis count in PR-5)
    assert body["monthly_spent_usd"] == 20.0
    assert body["monthly_budget_usd"] == 100.0


def test_status_viewer_reports_zero_not_unlimited(
    authed_client: tuple[TestClient, AsyncMock],
):
    """A no-access role must report daily_remaining=0 — null would read as uncapped."""
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_member("viewer")),
            _scalar_one_or_none(_llm_config()),
            _scalar_one(0.0),
            _scalar_one(0.0),
            _scalar_one(0.0),
        ]
    )

    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/llm/status")
    assert resp.status_code == 200
    assert resp.json()["daily_remaining"] == 0


def test_status_owner_unlimited(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_member("owner")),
            _scalar_one_or_none(_llm_config()),
            _scalar_one(0.0),
            _scalar_one(0.0),
            _scalar_one(0.0),
        ]
    )

    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/llm/status")
    assert resp.status_code == 200
    assert resp.json()["daily_remaining"] is None


def test_status_reports_budget_exhaustion(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_member("editor")),
            _scalar_one_or_none(_llm_config(monthly=50.0)),
            _scalar_one(50.0),  # monthly spend == budget
            _scalar_one(2.0),
            _scalar_one(10.0),
        ]
    )

    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/llm/status")
    assert resp.status_code == 200
    assert resp.json()["budget_exhausted"] is True


def test_status_local_provider_configured_without_key(
    authed_client: tuple[TestClient, AsyncMock],
):
    """Ollama-style local providers are usable with no stored API key."""
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_member("editor")),
            _scalar_one_or_none(
                _llm_config(provider="ollama", api_key_encrypted=None, monthly=None)
            ),
            _scalar_one(0.0),
            _scalar_one(0.0),
            _scalar_one(0.0),
        ]
    )

    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/llm/status")
    assert resp.status_code == 200
    assert resp.json()["configured"] is True


def test_status_response_never_leaks_key_material(
    authed_client: tuple[TestClient, AsyncMock],
):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_member("admin")),
            _scalar_one_or_none(_llm_config()),
            _scalar_one(0.0),
            _scalar_one(0.0),
            _scalar_one(0.0),
        ]
    )

    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/llm/status")
    assert resp.status_code == 200
    assert not any("key" in field.lower() for field in resp.json())


# ── PATCH /members/{id}/flags ─────────────────────────────────────────────────


def test_member_flags_requires_auth(client: TestClient):
    resp = client.patch(
        f"/api/v1/projects/{PROJECT_ID}/members/target-user/flags",
        json={"can_self_merge_structural": True},
    )
    assert resp.status_code in (401, 403)


def test_member_flags_403_for_editor(authed_client: tuple[TestClient, AsyncMock]):
    """The privilege-granting endpoint is owner/admin only — editors cannot self-grant."""
    client, session = authed_client
    session.execute = AsyncMock(side_effect=[_scalar_one_or_none(_member("editor"))])

    resp = client.patch(
        f"/api/v1/projects/{PROJECT_ID}/members/test-user-id/flags",
        json={"can_self_merge_structural": True},
    )
    assert resp.status_code == 403
    session.commit.assert_not_called()


def test_member_flags_403_for_non_member(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(side_effect=[_scalar_one_or_none(None)])

    resp = client.patch(
        f"/api/v1/projects/{PROJECT_ID}/members/target-user/flags",
        json={"can_self_merge_structural": True},
    )
    assert resp.status_code == 403


def test_member_flags_404_for_missing_target(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_member("owner")),  # actor role
            _scalar_one_or_none(None),  # target not a member
        ]
    )

    resp = client.patch(
        f"/api/v1/projects/{PROJECT_ID}/members/ghost-user/flags",
        json={"can_self_merge_structural": True},
    )
    assert resp.status_code == 404
    session.commit.assert_not_called()


def test_member_flags_admin_toggles_flag(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    target = Mock()
    target.user_id = "target-user"
    target.can_self_merge_structural = False
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_member("admin")),
            _scalar_one_or_none(target),
        ]
    )

    resp = client.patch(
        f"/api/v1/projects/{PROJECT_ID}/members/target-user/flags",
        json={"can_self_merge_structural": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"user_id": "target-user", "can_self_merge_structural": True}
    assert target.can_self_merge_structural is True
    session.commit.assert_awaited_once()


def test_member_flags_rejects_malformed_body(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(side_effect=[_scalar_one_or_none(_member("owner"))])

    resp = client.patch(
        f"/api/v1/projects/{PROJECT_ID}/members/target-user/flags",
        json={},
    )
    assert resp.status_code == 422
