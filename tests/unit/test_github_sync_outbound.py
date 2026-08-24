"""Tests for the outbound-only mirror and its credential (U10).

Outbound-only closes the review-bypass hole: without it, a change merged
directly on GitHub would sync back into the canonical ontology, around the
suggestion pipeline and its trust ladder entirely. The tests therefore assert
not just the reported status but that the canonical refs are never written.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services.demo_target_authorizer import DemoTargetDenied
from ontokit.services.github_sync import sync_github_project
from ontokit.services.mirror_credential import (
    PAT_FALLBACK_EVENT,
    resolve_mirror_credential,
)

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
BRANCH = "main"
SYSTEM_TOKEN = "ghp_system_mirror_identity"
USER_PAT = "ghp_per_user_token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _integration(connected_by: str | None = "user-1") -> MagicMock:
    integration = MagicMock()
    integration.project_id = PROJECT_ID
    integration.default_branch = BRANCH
    integration.sync_status = "idle"
    integration.sync_error = None
    integration.last_sync_at = None
    integration.connected_by_user_id = connected_by
    integration.repo_owner = "CatholicOS"
    integration.repo_name = "ontology-semantic-canon"
    integration.project = MagicMock(is_demo=False)
    return integration


def _pygit2_repo(*, ahead: int, behind: int, same: bool = False) -> MagicMock:
    repo = MagicMock()
    local_ref = MagicMock()
    local_ref.target = "local_oid"
    remote_ref = MagicMock()
    remote_ref.target = "local_oid" if same else "remote_oid"

    refs = MagicMock()

    def _getitem(key: str) -> MagicMock:
        if key == f"refs/heads/{BRANCH}":
            return local_ref
        if key == f"refs/remotes/origin/{BRANCH}":
            return remote_ref
        raise KeyError(key)

    refs.__getitem__ = MagicMock(side_effect=_getitem)
    repo.references = refs
    repo.ahead_behind.return_value = (ahead, behind)
    # Expose the local ref so tests can assert it was never re-pointed.
    repo._local_ref = local_ref
    return repo


def _git_service(*, push_ok: bool = True, ahead: int = 0, behind: int = 0, same: bool = False):
    service = MagicMock()
    service.repository_exists.return_value = True
    repo = MagicMock()
    repo.fetch.return_value = True
    repo.push.return_value = push_ok
    repo.repo = _pygit2_repo(ahead=ahead, behind=behind, same=same)
    service.get_repository.return_value = repo
    return service, repo


# ---------------------------------------------------------------------------
# Outbound-only direction
# ---------------------------------------------------------------------------


class TestOutboundOnlySync:
    async def test_local_ahead_pushes(self) -> None:
        integration = _integration()
        service, repo = _git_service(ahead=2, behind=0)
        result = await sync_github_project(
            integration, SYSTEM_TOKEN, service, AsyncMock(), outbound_only=True
        )
        assert result["status"] == "pushed"
        repo.push.assert_called_once()

    async def test_remote_ahead_reports_diverged_without_touching_local(self) -> None:
        """R16: the canonical repository is never advanced from the mirror."""
        integration = _integration()
        service, repo = _git_service(ahead=0, behind=3)
        result = await sync_github_project(
            integration, SYSTEM_TOKEN, service, AsyncMock(), outbound_only=True
        )
        assert result["status"] == "diverged"
        assert result["behind"] == 3
        assert integration.sync_status == "diverged"
        repo.repo._local_ref.set_target.assert_not_called()

    async def test_diverged_reports_without_merging(self) -> None:
        """R16: no merge commit is created from remote history."""
        integration = _integration()
        service, repo = _git_service(ahead=1, behind=2)
        with patch("ontokit.services.github_sync._try_merge") as try_merge:
            result = await sync_github_project(
                integration, SYSTEM_TOKEN, service, AsyncMock(), outbound_only=True
            )
        assert result["status"] == "diverged"
        try_merge.assert_not_called()
        repo.push.assert_not_called()

    async def test_diverged_error_message_is_actionable(self) -> None:
        integration = _integration()
        service, _ = _git_service(ahead=0, behind=1)
        await sync_github_project(
            integration, SYSTEM_TOKEN, service, AsyncMock(), outbound_only=True
        )
        assert "outbound-only" in integration.sync_error

    async def test_in_sync_is_idle(self) -> None:
        integration = _integration()
        service, repo = _git_service(same=True)
        result = await sync_github_project(
            integration, SYSTEM_TOKEN, service, AsyncMock(), outbound_only=True
        )
        assert result["status"] == "idle"
        assert result["reason"] == "up_to_date"
        repo.push.assert_not_called()

    async def test_push_failure_is_an_error(self) -> None:
        integration = _integration()
        service, _ = _git_service(ahead=1, behind=0, push_ok=False)
        result = await sync_github_project(
            integration, SYSTEM_TOKEN, service, AsyncMock(), outbound_only=True
        )
        assert result["status"] == "error"
        assert result["reason"] == "push_failed"

    async def test_direction_defaults_to_the_setting(self) -> None:
        """Omitting outbound_only must not silently fall back to bidirectional."""
        integration = _integration()
        service, repo = _git_service(ahead=0, behind=2)
        with patch("ontokit.services.github_sync.settings") as mock_settings:
            mock_settings.github_mirror_outbound_only = True
            result = await sync_github_project(integration, SYSTEM_TOKEN, service, AsyncMock())
        assert result["status"] == "diverged"
        repo.repo._local_ref.set_target.assert_not_called()

    async def test_escape_hatch_restores_bidirectional(self) -> None:
        """github_mirror_outbound_only=False keeps the legacy behavior."""
        integration = _integration()
        service, repo = _git_service(ahead=0, behind=2)
        with patch("ontokit.services.github_sync.settings") as mock_settings:
            mock_settings.github_mirror_outbound_only = False
            result = await sync_github_project(integration, SYSTEM_TOKEN, service, AsyncMock())
        assert result["status"] == "pulled"
        repo.repo._local_ref.set_target.assert_called_once()


# ---------------------------------------------------------------------------
# Mirror credential resolution
# ---------------------------------------------------------------------------


def _db(token_row: object = None) -> AsyncMock:
    db = AsyncMock()
    result = Mock()
    result.scalar_one_or_none.return_value = token_row
    db.execute = AsyncMock(return_value=result)
    return db


def _token_row(encrypted: str = "ciphertext") -> MagicMock:
    row = MagicMock()
    row.encrypted_token = encrypted
    return row


class TestResolveMirrorCredential:
    async def test_target_denial_remains_typed_for_the_caller(self) -> None:
        project = Project(
            id=PROJECT_ID,
            name="Live project",
            owner_id="owner",
            is_demo=False,
        )
        integration = GitHubIntegration(
            project_id=PROJECT_ID,
            repo_owner="alea-institute",
            repo_name="ontokit-demo-folio",
        )
        integration.project = project
        db = AsyncMock()

        with pytest.raises(DemoTargetDenied, match="live project cannot target demo"):
            await resolve_mirror_credential(db, integration)

        db.execute.assert_not_awaited()

    async def test_prefers_the_system_token(self) -> None:
        db = _db(_token_row())
        with patch("ontokit.services.mirror_credential.settings") as mock_settings:
            mock_settings.github_mirror_token = SYSTEM_TOKEN
            token = await resolve_mirror_credential(db, _integration())
        assert token == SYSTEM_TOKEN
        db.execute.assert_not_awaited()

    async def test_falls_back_to_the_stored_pat_with_a_deprecation_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        db = _db(_token_row())
        with (
            patch("ontokit.services.mirror_credential.settings") as mock_settings,
            patch("ontokit.services.mirror_credential.decrypt_token", return_value=USER_PAT),
            caplog.at_level("WARNING", logger="ontokit.services.mirror_credential"),
        ):
            mock_settings.github_mirror_token = ""
            token = await resolve_mirror_credential(db, _integration())
        assert token == USER_PAT
        assert any(PAT_FALLBACK_EVENT in r.message for r in caplog.records)

    async def test_returns_none_when_no_credential_exists(self) -> None:
        db = _db(None)
        with patch("ontokit.services.mirror_credential.settings") as mock_settings:
            mock_settings.github_mirror_token = ""
            assert await resolve_mirror_credential(db, _integration()) is None

    async def test_returns_none_when_the_integration_has_no_connecting_user(self) -> None:
        db = _db(_token_row())
        with patch("ontokit.services.mirror_credential.settings") as mock_settings:
            mock_settings.github_mirror_token = ""
            assert await resolve_mirror_credential(db, _integration(None)) is None

    async def test_decrypt_failure_returns_none_rather_than_raising(self) -> None:
        """One project's bad credential must not abort the sync sweep."""
        db = _db(_token_row())
        with (
            patch("ontokit.services.mirror_credential.settings") as mock_settings,
            patch(
                "ontokit.services.mirror_credential.decrypt_token",
                side_effect=RuntimeError("bad ciphertext"),
            ),
        ):
            mock_settings.github_mirror_token = ""
            assert await resolve_mirror_credential(db, _integration()) is None
