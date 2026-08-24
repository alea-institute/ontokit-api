"""Proof that demo GitHub writes cannot cross the live-project boundary."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ontokit.core.demo_targets import (
    DemoTargetAuthorization,
    is_demo_repository,
    repository_from_remote_url,
)
from ontokit.git.bare_repository import BareOntologyRepository
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services.demo_target_authorizer import (
    DemoTargetDenied,
    authorize_integration_target,
)
from ontokit.services.pr_party_github import ReviewEvent, actuation_client

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
DEMO_OWNER = "alea-institute"
DEMO_REPO = "ontokit-demo-folio"


def _db(project: object) -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = project
    db = AsyncMock()
    db.execute.return_value = result
    return db


def _demo_db(project: object) -> AsyncMock:
    project_result = MagicMock()
    project_result.scalar_one_or_none.return_value = project
    source_result = MagicMock()
    source_result.scalar_one_or_none.return_value = SimpleNamespace(
        repo_owner="alea-institute", repo_name="FOLIO"
    )
    db = AsyncMock()
    db.execute.side_effect = [project_result, source_result]
    return db


def _project(*, is_demo: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=PROJECT_ID,
        is_demo=is_demo,
        demo_source_project_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        if is_demo
        else None,
    )


def _integration(owner: str = DEMO_OWNER, repo: str = DEMO_REPO) -> GitHubIntegration:
    return GitHubIntegration(project_id=PROJECT_ID, repo_owner=owner, repo_name=repo)


class TestProjectAwareAuthorization:
    async def test_demo_project_uses_only_dedicated_token(self) -> None:
        with patch(
            "ontokit.services.demo_target_authorizer.settings.github_demo_mirror_token",
            "demo-token",
        ):
            authorization = await authorize_integration_target(
                _demo_db(_project(is_demo=True)),
                _integration(),  # type: ignore[arg-type]
                operation="test push",
            )

        assert authorization.token == "demo-token"
        assert authorization.capability is not None
        assert authorization.capability.permits(DEMO_OWNER, DEMO_REPO)

    async def test_demo_project_off_allowlist_is_refused(self) -> None:
        with pytest.raises(DemoTargetDenied, match="demo project cannot target"):
            await authorize_integration_target(
                _demo_db(_project(is_demo=True)),
                _integration("CatholicOS", "ontology-semantic-canon"),  # type: ignore[arg-type]
                operation="test push",
            )

    async def test_live_project_cannot_target_demo_repository(self) -> None:
        with pytest.raises(DemoTargetDenied, match="live project cannot target demo"):
            await authorize_integration_target(
                _db(_project(is_demo=False)),
                _integration(),  # type: ignore[arg-type]
                operation="test push",
            )

    async def test_missing_demo_token_fails_closed(self) -> None:
        with (
            patch("ontokit.services.demo_target_authorizer.settings.github_demo_mirror_token", ""),
            pytest.raises(DemoTargetDenied, match="GITHUB_DEMO_MIRROR_TOKEN"),
        ):
            await authorize_integration_target(
                _demo_db(_project(is_demo=True)),
                _integration(),  # type: ignore[arg-type]
                operation="test push",
            )

    async def test_demo_target_must_match_its_linked_live_source(self) -> None:
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = _project(is_demo=True)
        source_result = MagicMock()
        source_result.scalar_one_or_none.return_value = SimpleNamespace(
            repo_owner="CatholicOS", repo_name="ontology-semantic-canon"
        )
        db = AsyncMock()
        db.execute.side_effect = [project_result, source_result]
        with (
            patch(
                "ontokit.services.demo_target_authorizer.settings.github_demo_mirror_token",
                "demo-token",
            ),
            pytest.raises(DemoTargetDenied, match="does not match its live source"),
        ):
            await authorize_integration_target(
                db,
                _integration(),  # type: ignore[arg-type]
                operation="test push",
            )

    async def test_live_project_keeps_ordinary_credential_path(self) -> None:
        authorization = await authorize_integration_target(
            _db(_project(is_demo=False)),
            _integration("CatholicOS", "ontology-semantic-canon"),  # type: ignore[arg-type]
            operation="test push",
        )
        assert authorization.token is None
        assert authorization.capability is None


class TestLowLevelBoundary:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/alea-institute/ontokit-demo-folio.git",
            "git@github.com:alea-institute/ontokit-demo-folio.git",
        ],
    )
    def test_remote_parser_identifies_demo_targets(self, url: str) -> None:
        target = repository_from_remote_url(url)
        assert target is not None
        assert is_demo_repository(*target)

    def test_bare_push_refuses_demo_target_without_capability(self) -> None:
        repository = BareOntologyRepository.__new__(BareOntologyRepository)
        remote = MagicMock()
        remote.url = "https://github.com/alea-institute/ontokit-demo-folio.git"
        repository._repo = MagicMock()
        repository._repo.remotes.__getitem__.return_value = remote

        assert repository.push(token="operator-token") is False
        remote.push.assert_not_called()

    def test_bare_push_accepts_exact_capability(self) -> None:
        repository = BareOntologyRepository.__new__(BareOntologyRepository)
        remote = MagicMock()
        remote.url = "https://github.com/alea-institute/ontokit-demo-folio.git"
        repository._repo = MagicMock()
        repository._repo.remotes.__getitem__.return_value = remote
        repository.get_default_branch = MagicMock(return_value="main")  # type: ignore[method-assign]
        capability = DemoTargetAuthorization(PROJECT_ID, DEMO_OWNER, DEMO_REPO)

        assert repository.push(token="demo-token", target_authorization=capability) is True
        remote.push.assert_called_once()


class TestUnscopedPRPartyBoundary:
    async def test_actuation_refuses_demo_target_before_http(self) -> None:
        with patch("httpx.AsyncClient") as client, pytest.raises(PermissionError):
            await actuation_client("reviewer-token").create_review(
                DEMO_OWNER,
                DEMO_REPO,
                1,
                commit_id="a" * 40,
                event=ReviewEvent.APPROVE,
            )
        client.assert_not_called()
