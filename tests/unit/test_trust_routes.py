"""Tests for the trust-ladder admin routes.

Every endpoint here changes a privilege or the conditions under which content
merges without human review, so the authorization gate is the thing under test
as much as the behavior.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from ontokit.models.suggestion_outcome import SuggestionOutcome

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
OTHER_PROJECT_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
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
    """Every execute returns the project row, and every scalar the count."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = project
    result.scalar.return_value = accepted
    mock_session.execute = AsyncMock(return_value=result)


def _outcome(
    outcome_id: str,
    created_at: datetime,
    *,
    user_id: str | None = None,
    project_id: uuid.UUID = PROJECT_ID,
    snapshots: bool = True,
) -> SuggestionOutcome:
    row = SuggestionOutcome(
        id=uuid.UUID(outcome_id),
        project_id=project_id,
        user_id=user_id or f"user-{outcome_id[-1]}",
        outcome="accepted",
        is_anonymous=False,
        decided_by="reviewer-1",
        created_at=created_at,
    )
    if snapshots:
        row.submitter_name = "Contributor"
        row.submitter_email = "contributor@example.com"
        row.snapshot_tier = "trusted"
        row.snapshot_role = "suggester"
        row.snapshot_captured_at = created_at
        row.decided_by_name = "Project Reviewer"
    else:
        row.submitter_name = None
        row.submitter_email = None
        row.snapshot_tier = None
        row.snapshot_role = None
        row.snapshot_captured_at = None
        row.decided_by_name = None
    return row


def _outcome_results(
    project: MagicMock, rows: list[SuggestionOutcome], total: int
) -> list[MagicMock]:
    project_result = MagicMock()
    project_result.scalar_one_or_none.return_value = project
    count_result = MagicMock()
    count_result.scalar_one.return_value = total
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows
    return [project_result, count_result, rows_result]


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
        assert any("auto-accept ENABLED" in r.message for r in caplog.records)

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


class TestSuggestionOutcomeAudit:
    @pytest.mark.parametrize(
        ("project", "expected_status"),
        [
            (_project([], owner_id="test-user-id"), 200),
            (_project([_member("test-user-id", "admin")]), 200),
            (_project([_member("test-user-id", "editor")]), 403),
            (_project([_member("test-user-id", "member")]), 403),
            (_project([_member("test-user-id", "suggester")]), 403),
        ],
        ids=["owner", "admin", "editor", "member", "suggester"],
    )
    def test_access_is_limited_to_owner_and_admin(
        self,
        authed_client: tuple[TestClient, AsyncMock],
        project: MagicMock,
        expected_status: int,
    ) -> None:
        client, session = authed_client
        session.execute.side_effect = _outcome_results(project, [], 0)
        response = client.get(f"{BASE}/outcomes")
        assert response.status_code == expected_status
        if expected_status == 200:
            assert response.json() == {"items": [], "total": 0, "next_cursor": None}

    def test_unauthenticated_is_rejected(self, client: TestClient) -> None:
        assert client.get(f"{BASE}/outcomes").status_code == 401

    @pytest.mark.parametrize(
        "cursor",
        [
            "%%%not-base64%%%",
            base64.urlsafe_b64encode(b"\xff").decode(),
        ],
        ids=["invalid-base64", "invalid-utf8"],
    )
    def test_invalid_cursor_returns_typed_client_error(
        self,
        authed_client: tuple[TestClient, AsyncMock],
        admin_project: MagicMock,
        cursor: str,
    ) -> None:
        client, session = authed_client
        session.execute.side_effect = _outcome_results(admin_project, [], 0)

        response = client.get(f"{BASE}/outcomes", params={"cursor": cursor})

        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid outcome cursor"}

    def test_three_page_keyset_cursor_walk_survives_a_newer_insert(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        now = datetime(2026, 8, 10, 12, tzinfo=UTC)
        newest = _outcome("00000000-0000-0000-0000-000000000005", now)
        second = _outcome("00000000-0000-0000-0000-000000000004", now - timedelta(minutes=1))
        third = _outcome("00000000-0000-0000-0000-000000000003", now - timedelta(minutes=2))
        fourth = _outcome("00000000-0000-0000-0000-000000000002", now - timedelta(minutes=3))
        oldest = _outcome("00000000-0000-0000-0000-000000000001", now - timedelta(minutes=4))
        session.execute.side_effect = [
            *_outcome_results(admin_project, [newest, second, third], 5),
            *_outcome_results(admin_project, [third, fourth, oldest], 6),
            *_outcome_results(admin_project, [oldest], 6),
        ]

        first = client.get(f"{BASE}/outcomes", params={"limit": 2})
        assert first.status_code == 200
        assert [item["user_id"] for item in first.json()["items"]] == ["user-5", "user-4"]
        first_cursor = first.json()["next_cursor"]
        assert first_cursor is not None

        # The total grows because a newer row was inserted, but that row is before
        # the saved keyset boundary and cannot duplicate or displace older rows.
        second_page = client.get(
            f"{BASE}/outcomes", params={"limit": 2, "cursor": first_cursor}
        )
        assert second_page.status_code == 200
        assert second_page.json()["total"] == 6
        assert [item["user_id"] for item in second_page.json()["items"]] == [
            "user-3",
            "user-2",
        ]
        second_cursor = second_page.json()["next_cursor"]
        assert second_cursor is not None

        third_page = client.get(
            f"{BASE}/outcomes", params={"limit": 2, "cursor": second_cursor}
        )
        assert third_page.status_code == 200
        assert [item["user_id"] for item in third_page.json()["items"]] == ["user-1"]
        assert third_page.json()["next_cursor"] is None

        walked_ids = [
            item["user_id"]
            for page in (first, second_page, third_page)
            for item in page.json()["items"]
        ]
        assert walked_ids == ["user-5", "user-4", "user-3", "user-2", "user-1"]
        assert len(walked_ids) == len(set(walked_ids))

    def test_cursor_cannot_be_replayed_across_projects(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        now = datetime(2026, 8, 10, 12, tzinfo=UTC)
        newest = _outcome("00000000-0000-0000-0000-000000000002", now)
        older = _outcome(
            "00000000-0000-0000-0000-000000000001", now - timedelta(minutes=1)
        )
        session.execute.side_effect = [
            *_outcome_results(admin_project, [newest, older], 2),
            *_outcome_results(admin_project, [], 0),
        ]

        first = client.get(f"{BASE}/outcomes", params={"limit": 1})
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        assert cursor is not None

        replay = client.get(
            f"/api/v1/projects/{OTHER_PROJECT_ID}/trust/outcomes",
            params={"cursor": cursor},
        )

        assert replay.status_code == 422
        assert replay.json() == {"detail": "Invalid outcome cursor"}

    def test_query_asserts_tenant_filter_and_cursor_ordering(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        timestamp = datetime(2026, 8, 10, 12, tzinfo=UTC)
        same_time_high_id = _outcome("00000000-0000-0000-0000-000000000002", timestamp)
        same_time_low_id = _outcome("00000000-0000-0000-0000-000000000001", timestamp)
        session.execute.side_effect = _outcome_results(
            admin_project, [same_time_high_id, same_time_low_id], 2
        )

        response = client.get(f"{BASE}/outcomes")
        assert response.status_code == 200
        assert [item["user_id"] for item in response.json()["items"]] == ["user-2", "user-1"]

        count_query = session.execute.call_args_list[1].args[0]
        page_query = session.execute.call_args_list[2].args[0]
        assert count_query.compile().params == {"project_id_1": PROJECT_ID}
        assert page_query.compile().params["project_id_1"] == PROJECT_ID
        assert "suggestion_outcomes.project_id" in str(page_query)
        assert "ORDER BY suggestion_outcomes.created_at DESC, suggestion_outcomes.id DESC" in str(
            page_query
        )

    def test_other_project_rows_are_not_returned(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        timestamp = datetime(2026, 8, 10, 12, tzinfo=UTC)
        project_row = _outcome("00000000-0000-0000-0000-000000000001", timestamp, user_id="ours")
        session.execute.side_effect = _outcome_results(admin_project, [project_row], 1)

        body = client.get(f"{BASE}/outcomes").json()
        assert [item["user_id"] for item in body["items"]] == ["ours"]
        assert body["total"] == 1
        page_query = session.execute.call_args_list[2].args[0]
        assert page_query.compile().params["project_id_1"] == PROJECT_ID

    def test_pre_snapshot_row_serializes_nulls_and_only_audit_fields(
        self, authed_client: tuple[TestClient, AsyncMock], admin_project: MagicMock
    ) -> None:
        client, session = authed_client
        row = _outcome(
            "00000000-0000-0000-0000-000000000001",
            datetime(2026, 8, 10, 12, tzinfo=UTC),
            snapshots=False,
        )
        session.execute.side_effect = _outcome_results(admin_project, [row], 1)

        item = client.get(f"{BASE}/outcomes").json()["items"][0]
        assert set(item) == {
            "user_id",
            "is_anonymous",
            "submitter_name",
            "submitter_email",
            "snapshot_tier",
            "snapshot_role",
            "snapshot_captured_at",
            "outcome",
            "decided_by",
            "decided_by_name",
            "created_at",
        }
        assert item["submitter_name"] is None
        assert item["submitter_email"] is None
        assert item["snapshot_tier"] is None
        assert item["snapshot_role"] is None
        assert item["snapshot_captured_at"] is None
        assert item["decided_by_name"] is None

    @pytest.mark.parametrize("limit", [0, 101])
    def test_limit_bounds_are_validated(
        self, authed_client: tuple[TestClient, AsyncMock], limit: int
    ) -> None:
        client, _ = authed_client
        assert client.get(f"{BASE}/outcomes", params={"limit": limit}).status_code == 422

    @pytest.mark.parametrize(
        "payload",
        [
            "2026-08-10T12:00:00|00000000-0000-0000-0000-000000000001",
            "2026-08-10T12:00:00+00:00|not-a-uuid",
            "2026-08-10T12:00:00+00:00",
        ],
        ids=["naive-timestamp", "bad-uuid", "missing-separator"],
    )
    def test_malformed_cursor_deep_branches_are_rejected(
        self,
        authed_client: tuple[TestClient, AsyncMock],
        admin_project: MagicMock,
        payload: str,
    ) -> None:
        client, session = authed_client
        session.execute.side_effect = _outcome_results(admin_project, [], 0)
        cursor = base64.urlsafe_b64encode(payload.encode()).decode()
        assert client.get(f"{BASE}/outcomes", params={"cursor": cursor}).status_code == 422
