"""Tests for the PR Party reviewer settings routes (U2).

The contract these pin:

- ``GET /pr-party/me`` is a *capability* read. A non-reviewer gets a 200 with
  ``is_reviewer: false`` (the web client gates its nav on it, so 403-ing the
  read itself would make "am I a reviewer?" unanswerable). Every other route is
  403 for a non-reviewer and 401 unauthenticated.
- ``ntfy_topic`` is a secret and never appears in the capability payload —
  only in the reviewer's own settings read.
- A missing credential is *degraded*, not an error: the dashboard still renders
  and the reviewer is told to connect a PAT.
- KTD19: with ``AUTH_MODE=disabled`` there is no per-user identity to bind a
  write PAT to, so the router is not mounted at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from ontokit.api.routes import include_pr_party_routes
from ontokit.api.routes.pr_party_settings import get_credential_service
from ontokit.main import app
from ontokit.models.pr_party import PRPartyCredential, PRPartyMergeDefault, PRPartyReviewer
from ontokit.schemas.pr_party import PRPartyGenerationTokenStatus
from ontokit.services.pr_party_credentials import (
    GITHUB_TOKEN_SETTINGS_URL,
    CredentialRejected,
    CredentialValidationUnavailable,
    credential_health,
)

BASE = "/api/v1/pr-party"
USER_ID = "test-user-id"


def _reviewer(login: str = "octocat", ntfy_topic: str | None = None) -> PRPartyReviewer:
    row = PRPartyReviewer(
        zitadel_user_id=USER_ID,
        github_login=login,
        github_node_id="MDQ6VXNl",
        merge_default=PRPartyMergeDefault.MANUAL,
        ntfy_topic=ntfy_topic,
    )
    row.id = uuid.uuid4()
    return row


def _credential(
    reviewer: PRPartyReviewer,
    *,
    expires_at: datetime | None = None,
    last_error: str | None = None,
) -> PRPartyCredential:
    row = PRPartyCredential(
        reviewer_id=reviewer.id,
        encrypted_token="ciphertext",
        expires_at=expires_at,
        last_validated_at=datetime.now(UTC) - timedelta(hours=2),
        last_error=last_error,
    )
    row.id = uuid.uuid4()
    return row


class _FakeService:
    """Stands in for PRPartyCredentialService at the route boundary."""

    def __init__(
        self,
        reviewer: PRPartyReviewer | None = None,
        credential: PRPartyCredential | None = None,
        *,
        save_error: Exception | None = None,
    ) -> None:
        self.reviewer = reviewer
        self.credential = credential
        self.save_error = save_error
        self.saved_tokens: list[str] = []
        self.deleted = 0
        self.updates: list[dict[str, Any]] = []

    async def get_reviewer(self, zitadel_user_id: str) -> PRPartyReviewer | None:
        if self.reviewer is not None and self.reviewer.zitadel_user_id == zitadel_user_id:
            return self.reviewer
        return None

    async def get_credential(self, _reviewer_id: uuid.UUID) -> PRPartyCredential | None:
        return self.credential

    async def save_credential(self, reviewer: PRPartyReviewer, token: str) -> PRPartyCredential:  # noqa: ARG002
        if self.save_error is not None:
            raise self.save_error
        self.saved_tokens.append(token)
        self.credential = self.credential or _credential(reviewer)
        self.credential.last_error = None
        return self.credential

    async def delete_credential(self, _reviewer: PRPartyReviewer) -> bool:
        self.deleted += 1
        existed = self.credential is not None
        self.credential = None
        return existed

    async def update_settings(
        self, reviewer: PRPartyReviewer, updates: dict[str, Any]
    ) -> PRPartyReviewer:
        self.updates.append(dict(updates))
        for field, value in updates.items():
            setattr(reviewer, field, value)
        return reviewer


@pytest.fixture
def wired(
    authed_client: tuple[TestClient, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """(client, install) — ``install(service)`` binds a fake service to the routes."""
    client, _db = authed_client

    async def _no_generation_status(**_kwargs: Any) -> PRPartyGenerationTokenStatus | None:
        return None

    monkeypatch.setattr(
        "ontokit.api.routes.pr_party_settings.get_generation_token_status",
        _no_generation_status,
    )

    def install(service: _FakeService) -> _FakeService:
        app.dependency_overrides[get_credential_service] = lambda: service
        return service

    return client, install


# ---------------------------------------------------------------------------
# GET /pr-party/me
# ---------------------------------------------------------------------------


class TestCapability:
    def test_non_reviewer_gets_minimal_payload(self, wired: Any) -> None:
        client, install = wired
        install(_FakeService(reviewer=None))

        response = client.get(f"{BASE}/me")

        assert response.status_code == 200
        body = response.json()
        assert body["is_reviewer"] is False
        assert body["github_login"] is None
        assert body["credential"] is None
        assert body["degraded"] is False

    def test_reviewer_with_healthy_credential(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        install(
            _FakeService(
                reviewer,
                _credential(reviewer, expires_at=datetime.now(UTC) + timedelta(days=200)),
            )
        )

        body = client.get(f"{BASE}/me").json()

        assert body["is_reviewer"] is True
        assert body["github_login"] == "octocat"
        assert body["degraded"] is False
        assert body["credential"]["expired"] is False
        assert body["credential"]["last_validated_at"] is not None

    def test_missing_credential_is_degraded_not_an_error(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        install(_FakeService(reviewer, credential=None))

        response = client.get(f"{BASE}/me")

        assert response.status_code == 200
        body = response.json()
        assert body["is_reviewer"] is True
        assert body["degraded"] is True
        assert body["credential"] is None

    def test_expired_credential_surfaces_and_degrades(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        expired_at = datetime.now(UTC) - timedelta(days=2)
        install(_FakeService(reviewer, _credential(reviewer, expires_at=expired_at)))

        body = client.get(f"{BASE}/me").json()

        assert body["degraded"] is True
        assert body["credential"]["expired"] is True
        assert body["credential"]["expires_at"] is not None

    def test_stored_error_degrades(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        install(_FakeService(reviewer, _credential(reviewer, last_error="login_changed")))

        body = client.get(f"{BASE}/me").json()

        assert body["degraded"] is True
        assert body["credential"]["last_error"] == "login_changed"

    def test_ntfy_topic_never_in_capability_payload(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer(ntfy_topic="super-secret-topic")
        install(_FakeService(reviewer, _credential(reviewer)))

        response = client.get(f"{BASE}/me")

        assert "ntfy_topic" not in response.json()
        assert "super-secret-topic" not in response.text

    def test_generation_token_status_included_for_reviewers(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, install = wired
        reviewer = _reviewer()
        install(_FakeService(reviewer, _credential(reviewer)))
        expires = datetime.now(UTC) + timedelta(days=3)

        async def _status(**_kwargs: Any) -> PRPartyGenerationTokenStatus:
            return PRPartyGenerationTokenStatus(expires_at=expires, last_error=None)

        monkeypatch.setattr(
            "ontokit.api.routes.pr_party_settings.get_generation_token_status", _status
        )

        body = client.get(f"{BASE}/me").json()

        assert body["generation_token"]["expires_at"] is not None

    def test_unauthenticated_is_401(self) -> None:
        # No auth override installed: RequiredUser rejects the anonymous caller
        # before any reviewer lookup happens.
        app.dependency_overrides.clear()
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get(f"{BASE}/me").status_code == 401
        assert client.get(f"{BASE}/settings").status_code == 401
        assert client.put(f"{BASE}/credential", json={"token": "x"}).status_code == 401


# ---------------------------------------------------------------------------
# PUT / DELETE /pr-party/credential
# ---------------------------------------------------------------------------


class TestCredentialRoutes:
    def test_save_returns_health(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        service = install(_FakeService(reviewer))

        response = client.put(f"{BASE}/credential", json={"token": "github_pat_11ABC"})

        assert response.status_code == 200
        assert service.saved_tokens == ["github_pat_11ABC"]
        assert response.json()["last_error"] is None

    def test_rejected_token_is_400(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        install(
            _FakeService(
                reviewer,
                save_error=CredentialRejected("Token belongs to someone-else, not octocat."),
            )
        )

        response = client.put(f"{BASE}/credential", json={"token": "github_pat_wrong"})

        assert response.status_code == 400
        assert "octocat" in response.text

    def test_github_outage_is_502(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        install(
            _FakeService(reviewer, save_error=CredentialValidationUnavailable("GitHub unreachable"))
        )

        response = client.put(f"{BASE}/credential", json={"token": "github_pat_ok"})

        assert response.status_code == 502

    def test_blank_token_rejected_by_schema(self, wired: Any) -> None:
        client, install = wired
        install(_FakeService(_reviewer()))

        assert client.put(f"{BASE}/credential", json={"token": ""}).status_code == 422

    def test_non_reviewer_forbidden(self, wired: Any) -> None:
        client, install = wired
        install(_FakeService(reviewer=None))

        response = client.put(f"{BASE}/credential", json={"token": "github_pat_11ABC"})

        assert response.status_code == 403

    def test_delete_returns_revoke_url(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        service = install(_FakeService(reviewer, _credential(reviewer)))

        response = client.delete(f"{BASE}/credential")

        assert response.status_code == 200
        assert service.deleted == 1
        # The app cannot revoke server-side (KTD13) — it points at GitHub.
        assert response.json()["revoke_url"] == GITHUB_TOKEN_SETTINGS_URL

    def test_delete_non_reviewer_forbidden(self, wired: Any) -> None:
        client, install = wired
        install(_FakeService(reviewer=None))

        assert client.delete(f"{BASE}/credential").status_code == 403


# ---------------------------------------------------------------------------
# GET / PUT /pr-party/settings
# ---------------------------------------------------------------------------


class TestSettingsRoutes:
    def test_get_returns_own_topic(self, wired: Any) -> None:
        client, install = wired
        install(_FakeService(_reviewer(ntfy_topic="my-topic")))

        body = client.get(f"{BASE}/settings").json()

        assert body["ntfy_topic"] == "my-topic"
        assert body["merge_default"] == "manual"
        assert body["github_login"] == "octocat"

    def test_put_saves_topic_and_merge_default(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        service = install(_FakeService(reviewer))

        response = client.put(
            f"{BASE}/settings",
            json={"ntfy_topic": "pr-party-alerts", "merge_default": "dashboard"},
        )

        assert response.status_code == 200
        assert service.updates == [
            {"ntfy_topic": "pr-party-alerts", "merge_default": PRPartyMergeDefault.DASHBOARD}
        ]
        assert response.json()["ntfy_topic"] == "pr-party-alerts"

    def test_put_partial_leaves_other_field_alone(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer(ntfy_topic="keep-me")
        service = install(_FakeService(reviewer))

        client.put(f"{BASE}/settings", json={"merge_default": "dashboard"})

        assert service.updates == [{"merge_default": PRPartyMergeDefault.DASHBOARD}]

    def test_empty_topic_clears_it(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer(ntfy_topic="keep-me")
        service = install(_FakeService(reviewer))

        client.put(f"{BASE}/settings", json={"ntfy_topic": ""})

        assert service.updates == [{"ntfy_topic": None}]

    def test_topic_with_path_separators_rejected(self, wired: Any) -> None:
        """The topic is concatenated into an ntfy URL — no path escapes."""
        client, install = wired
        install(_FakeService(_reviewer()))

        response = client.put(f"{BASE}/settings", json={"ntfy_topic": "../../admin"})

        assert response.status_code == 422

    def test_settings_write_targets_only_the_caller(self, wired: Any) -> None:
        """R23: a body-named reviewer is not a thing — unknown fields are refused."""
        client, install = wired
        reviewer = _reviewer()
        service = install(_FakeService(reviewer))

        client.put(
            f"{BASE}/settings",
            json={"ntfy_topic": "mine", "zitadel_user_id": "someone-else"},
        )

        assert service.updates == [{"ntfy_topic": "mine"}]
        assert reviewer.zitadel_user_id == USER_ID

    def test_non_reviewer_forbidden(self, wired: Any) -> None:
        client, install = wired
        install(_FakeService(reviewer=None))

        assert client.get(f"{BASE}/settings").status_code == 403
        assert client.put(f"{BASE}/settings", json={"ntfy_topic": "x"}).status_code == 403


# ---------------------------------------------------------------------------
# KTD19: mounting gate
# ---------------------------------------------------------------------------


class TestRouterGating:
    """Probe a throwaway app, so the gate is tested by behavior, not by wiring."""

    @staticmethod
    def _probe(auth_mode: str) -> tuple[bool, int]:
        target = APIRouter()
        mounted = include_pr_party_routes(target, auth_mode=auth_mode)
        probe_app = FastAPI()
        probe_app.include_router(target, prefix="/api/v1")
        app.dependency_overrides.clear()
        client = TestClient(probe_app, raise_server_exceptions=False)
        return mounted, client.get(f"{BASE}/me").status_code

    def test_not_mounted_when_auth_disabled(self) -> None:
        """KTD19: no per-user identity means no safe way to hold a write PAT."""
        mounted, status_code = self._probe("disabled")

        assert mounted is False
        assert status_code == 404

    def test_mounted_when_auth_required(self) -> None:
        mounted, status_code = self._probe("required")

        assert mounted is True
        # Present, and gated: 401 rather than 404.
        assert status_code == 401

    def test_mounted_in_the_live_app(self) -> None:
        paths = app.openapi()["paths"]

        assert f"{BASE}/me" in paths
        assert f"{BASE}/credential" in paths
        assert f"{BASE}/settings" in paths


# ---------------------------------------------------------------------------
# Health helper reuse (the route and the service agree)
# ---------------------------------------------------------------------------


def test_route_and_service_share_one_health_shape() -> None:
    reviewer = _reviewer()
    health = credential_health(_credential(reviewer, expires_at=None))
    assert health is not None
    assert health.model_dump().keys() == {
        "expires_at",
        "last_validated_at",
        "last_error",
        "expired",
        "expires_soon",
    }
