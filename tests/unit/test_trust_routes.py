"""Tests for the trust-ladder admin routes.

Every endpoint here changes a privilege or the conditions under which content
merges without human review, so the authorization gate is the thing under test
as much as the behavior.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from ontokit.api.routes.trust import update_trust_settings
from ontokit.core.auth import CurrentUser
from ontokit.schemas.trust import ProjectTrustSettingsUpdate

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
BASE = f"/api/v1/projects/{PROJECT_ID}/trust"


def _member(
    user_id: str,
    role: str = "suggester",
    *,
    is_trusted: bool = False,
    trust_override: str = "none",
) -> MagicMock:
    member = MagicMock()
    member.user_id = user_id
    member.role = role
    member.is_trusted = is_trusted
    member.trust_override = trust_override
    member.trust_granted_at = None
    member.trust_granted_by = None
    return member


def _project(
    members: list[MagicMock],
    *,
    owner_id: str = "someone-else",
    threshold: int = 5,
    auto_accept: bool = False,
    quiet_days: int = 7,
) -> MagicMock:
    project = MagicMock()
    project.id = PROJECT_ID
    project.owner_id = owner_id
    project.members = members
    project.trust_promotion_threshold = threshold
    project.auto_accept_enabled = auto_accept
    project.auto_accept_quiet_days = quiet_days
    return project


def _wire(mock_session: AsyncMock, project: MagicMock, accepted: int = 0) -> None:
    """Return the project first, then trust counts for follow-on queries."""
    project_result = MagicMock()
    project_result.scalar_one_or_none.return_value = project
    count_result = MagicMock()
    count_result.scalar.return_value = accepted
    count_result.all.return_value = [
        (member.user_id, accepted) for member in getattr(project, "members", [])
    ]
    mock_session.execute = AsyncMock(side_effect=[project_result, *([count_result] * 5)])


@pytest.fixture
def admin_project() -> MagicMock:
    """A project on which the authenticated test user is an admin."""
    return _project([_member("test-user-id", "admin"), _member("contributor-1")])


class TestListMemberTrust:
    def test_admin_sees_every_member(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        _wire(session, admin_project, accepted=3)
        response = client.get(f"{BASE}/members")
        assert response.status_code == 200
        rows = response.json()
        assert {r["user_id"] for r in rows} == {"test-user-id", "contributor-1"}
        by_id = {r["user_id"]: r for r in rows}
        assert by_id["test-user-id"]["tier"] == "reviewer"
        assert by_id["contributor-1"]["tier"] == "untrusted"
        assert by_id["contributor-1"]["accepted_count"] == 3
        assert session.execute.await_count == 2

    def test_non_admin_is_refused(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        client, session = authed_client
        _wire(session, _project([_member("test-user-id", "suggester")]))
        assert client.get(f"{BASE}/members").status_code == 403

    def test_owner_without_a_membership_row_is_allowed(
        self, authed_client: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = authed_client
        _wire(session, _project([], owner_id="test-user-id"))
        assert client.get(f"{BASE}/members").status_code == 200

    def test_missing_project_is_404(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        client, session = authed_client
        _wire(session, None)  # type: ignore[arg-type]
        assert client.get(f"{BASE}/members").status_code == 404


class TestUpdateMemberTrust:
    def test_grant_marks_the_member_trusted(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        _wire(session, admin_project)
        response = client.patch(f"{BASE}/members/contributor-1", json={"trust_override": "granted"})
        assert response.status_code == 200
        assert response.json()["is_trusted"] is True
        assert response.json()["tier"] == "trusted"

    def test_revoke_clears_trust(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        client, session = authed_client
        project = _project(
            [_member("test-user-id", "admin"), _member("contributor-1", is_trusted=True)]
        )
        _wire(session, project)
        response = client.patch(f"{BASE}/members/contributor-1", json={"trust_override": "revoked"})
        assert response.status_code == 200
        assert response.json()["is_trusted"] is False
        assert response.json()["tier"] == "untrusted"

    def test_unknown_member_is_404(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        _wire(session, admin_project)
        response = client.patch(f"{BASE}/members/ghost", json={"trust_override": "granted"})
        assert response.status_code == 404

    def test_non_admin_is_refused(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        client, session = authed_client
        _wire(session, _project([_member("test-user-id", "editor")]))
        response = client.patch(f"{BASE}/members/contributor-1", json={"trust_override": "granted"})
        assert response.status_code == 403

    def test_invalid_override_value_is_rejected(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        _wire(session, admin_project)
        response = client.patch(
            f"{BASE}/members/contributor-1", json={"trust_override": "promoted"}
        )
        assert response.status_code == 422

    def test_user_id_with_url_characters_is_handled(
        self, authed_client: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = authed_client
        weird = "user@example.com"
        _wire(session, _project([_member("test-user-id", "admin"), _member(weird)]))
        response = client.patch(
            f"{BASE}/members/{quote(weird, safe='')}", json={"trust_override": "granted"}
        )
        assert response.status_code == 200


class TestTrustSettings:
    def test_read_returns_the_ktd8_defaults(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        _wire(session, admin_project)
        body = client.get(f"{BASE}/settings").json()
        assert body == {
            "trust_promotion_threshold": 5,
            "auto_accept_enabled": False,
            "auto_accept_quiet_days": 7,
        }

    def test_partial_update_leaves_other_fields_alone(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        _wire(session, admin_project)
        body = client.patch(f"{BASE}/settings", json={"trust_promotion_threshold": 3}).json()
        assert body["trust_promotion_threshold"] == 3
        assert body["auto_accept_enabled"] is False

    def test_enabling_auto_accept_is_audited(
        self,
        authed_client: tuple[TestClient, AsyncMock],
        admin_project: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The single most consequential switch in the feature."""
        client, session = authed_client
        _wire(session, admin_project)
        with caplog.at_level("INFO", logger="ontokit.api.routes.trust"):
            response = client.patch(f"{BASE}/settings", json={"auto_accept_enabled": True})
        assert response.status_code == 200
        events = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "project_trust_settings_changed"
        ]
        assert len(events) == 1
        assert events[0].changed_fields == ("auto_accept_enabled",)

    def test_threshold_and_quiet_days_emit_one_metadata_audit_event(
        self,
        authed_client: tuple[TestClient, AsyncMock],
        admin_project: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, session = authed_client
        _wire(session, admin_project)
        with caplog.at_level("INFO", logger="ontokit.api.routes.trust"):
            response = client.patch(
                f"{BASE}/settings",
                json={"trust_promotion_threshold": 3, "auto_accept_quiet_days": 14},
            )
        assert response.status_code == 200
        events = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "project_trust_settings_changed"
        ]
        assert len(events) == 1
        assert events[0].project_id == str(PROJECT_ID)
        assert events[0].actor_id == "test-user-id"
        assert events[0].changed_fields == (
            "trust_promotion_threshold",
            "auto_accept_quiet_days",
        )

    def test_no_op_update_emits_no_change_event(
        self,
        authed_client: tuple[TestClient, AsyncMock],
        admin_project: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, session = authed_client
        _wire(session, admin_project)
        with caplog.at_level("INFO", logger="ontokit.api.routes.trust"):
            response = client.patch(
                f"{BASE}/settings",
                json={
                    "trust_promotion_threshold": 5,
                    "auto_accept_enabled": False,
                    "auto_accept_quiet_days": 7,
                },
            )
        assert response.status_code == 200
        assert not any(
            getattr(record, "event", None) == "project_trust_settings_changed"
            for record in caplog.records
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {"trust_promotion_threshold": 0},
            {"trust_promotion_threshold": 100000},
            {"auto_accept_quiet_days": 0},
            {"auto_accept_quiet_days": 5000},
        ],
    )
    def test_out_of_range_values_are_rejected(
        self,
        authed_client: tuple[TestClient, AsyncMock],
        admin_project: MagicMock,
        payload: dict[str, int],
    ) -> None:
        client, session = authed_client
        _wire(session, admin_project)
        assert client.patch(f"{BASE}/settings", json=payload).status_code == 422

    def test_non_admin_cannot_read_or_write(
        self, authed_client: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = authed_client
        _wire(session, _project([_member("test-user-id", "viewer")]))
        assert client.get(f"{BASE}/settings").status_code == 403
        assert (
            client.patch(f"{BASE}/settings", json={"auto_accept_enabled": True}).status_code == 403
        )


@pytest.mark.asyncio
async def test_effective_policy_changes_emit_one_metadata_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    project = _project([], owner_id="owner-1")
    db = AsyncMock()
    user = CurrentUser(id="owner-1")
    with (
        patch(
            "ontokit.api.routes.trust._load_project",
            new=AsyncMock(return_value=project),
        ),
        caplog.at_level("INFO", logger="ontokit.api.routes.trust"),
    ):
        await update_trust_settings(
            PROJECT_ID,
            ProjectTrustSettingsUpdate(
                trust_promotion_threshold=3,
                auto_accept_quiet_days=14,
            ),
            db,
            user,
        )

    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "project_trust_settings_changed"
    ]
    assert len(events) == 1
    assert events[0].project_id == str(PROJECT_ID)
    assert events[0].actor_id == "owner-1"
    assert events[0].changed_fields == (
        "trust_promotion_threshold",
        "auto_accept_quiet_days",
    )


@pytest.mark.asyncio
async def test_no_op_policy_patch_emits_no_change_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    project = _project([], owner_id="owner-1")
    db = AsyncMock()
    user = CurrentUser(id="owner-1")
    with (
        patch(
            "ontokit.api.routes.trust._load_project",
            new=AsyncMock(return_value=project),
        ),
        caplog.at_level("INFO", logger="ontokit.api.routes.trust"),
    ):
        await update_trust_settings(
            PROJECT_ID,
            ProjectTrustSettingsUpdate(
                trust_promotion_threshold=5,
                auto_accept_enabled=False,
                auto_accept_quiet_days=7,
            ),
            db,
            user,
        )

    assert not any(
        getattr(record, "event", None) == "project_trust_settings_changed"
        for record in caplog.records
    )
