"""No commit written on a user's behalf carries their real email (hosted UAT).

Git history is permanent and mirrored to public GitHub forks. Suggestion
commits already used the commit-identity resolver, but merges, direct source
saves, normalization, and project import authored commits with the account's
real address. Every user-derived author now resolves through
``resolve_commit_author``: the noreply alias by default, a verified address
only on opt-in, and the OntoKit system identity for system actors.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes.projects import save_source_content
from ontokit.core.auth import CurrentUser
from ontokit.core.constants import ONTOKIT_COMMITTER_EMAIL
from ontokit.git import BareGitRepositoryService
from ontokit.schemas.project import SourceContentSave
from ontokit.schemas.pull_request import PRMergeRequest
from ontokit.services.commit_identity import (
    CommitIdentityService,
    noreply_alias,
    resolve_commit_author,
)
from ontokit.services.normalization_service import NormalizationService
from ontokit.services.pull_request_service import PullRequestService
from ontokit.services.trust_service import SYSTEM_AUTO_ACCEPT_ACTOR

ROOT = Path(__file__).parents[2]
REAL_EMAIL = "uat-d10-owner@example.com"
VERIFIED_EMAIL = "owner@users.noreply.github.com"
USER = CurrentUser(id="owner-id", name="UAT D10 owner", email=REAL_EMAIL)
ALIAS = noreply_alias(USER.id, USER.name)

BASE = b"""\
@prefix ex: <https://example.test/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Ontology a owl:Ontology .
"""


def _preference(*, opted_in: bool) -> SimpleNamespace | None:
    if not opted_in:
        return None
    return SimpleNamespace(
        use_verified_email=True, commit_email_verified=True, commit_email=VERIFIED_EMAIL
    )


@pytest.fixture
def no_preference() -> Iterator[None]:
    with patch.object(CommitIdentityService, "get_preference", AsyncMock(return_value=None)):
        yield


@pytest.fixture
def verified_preference() -> Iterator[None]:
    with patch.object(
        CommitIdentityService,
        "get_preference",
        AsyncMock(return_value=_preference(opted_in=True)),
    ):
        yield


# ── Resolver ─────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("no_preference")
async def test_resolver_defaults_to_noreply_alias() -> None:
    name, email = await resolve_commit_author(AsyncMock(), USER)
    assert (name, email) == ("UAT D10 owner", ALIAS)
    assert email != REAL_EMAIL


@pytest.mark.usefixtures("verified_preference")
async def test_resolver_honors_verified_opt_in() -> None:
    assert await resolve_commit_author(AsyncMock(), USER) == ("UAT D10 owner", VERIFIED_EMAIL)


async def test_resolver_ignores_unverified_opt_in() -> None:
    unverified = SimpleNamespace(
        use_verified_email=True, commit_email_verified=False, commit_email=REAL_EMAIL
    )
    with patch.object(CommitIdentityService, "get_preference", AsyncMock(return_value=unverified)):
        assert await resolve_commit_author(AsyncMock(), USER) == ("UAT D10 owner", ALIAS)


async def test_resolver_uses_system_identity_for_system_actor() -> None:
    system = CurrentUser(id=SYSTEM_AUTO_ACCEPT_ACTOR, name="OntoKit auto-accept", email=None)
    db = AsyncMock()
    # None makes the repository fall back to the OntoKit committer address.
    assert await resolve_commit_author(db, system) == ("OntoKit auto-accept", None)
    db.execute.assert_not_awaited()


async def test_resolver_without_user_uses_system_identity() -> None:
    assert await resolve_commit_author(AsyncMock(), None) == (None, None)


# ── Real-git call sites: source save and normalization ───────────────────────


def _db() -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


class _Ontology:
    def __init__(self) -> None:
        self.loaded: set[tuple[Any, str]] = set()

    def is_loaded(self, project_id: Any, branch: str) -> bool:
        return (project_id, branch) in self.loaded

    async def load_from_git(self, project_id: Any, branch: str, *_args: Any) -> Any:
        from rdflib import Graph

        self.loaded.add((project_id, branch))
        return Graph()

    async def _get_graph(self, *_args: Any) -> Any:
        from rdflib import Graph

        return Graph()

    def unload(self, project_id: Any, branch: str) -> None:
        self.loaded.discard((project_id, branch))


class _Storage:
    def __init__(self) -> None:
        self.current = BASE

    async def upload_file(self, _path: str, content: bytes, _content_type: str) -> str:
        self.current = content
        return "source/ontology.ttl"

    async def download_file(self, _path: str) -> bytes:
        return self.current


async def _source_save(tmp_path: Path) -> tuple[BareGitRepositoryService, Any]:
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = git.initialize_repository(project_id, BASE, "ontology.ttl")
    project = SimpleNamespace(
        user_role="owner", source_file_path="ontology.ttl", git_ontology_path=None
    )
    with (
        patch("ontokit.api.routes.projects.get_arq_pool", AsyncMock(return_value=None)),
        patch("ontokit.services.translation_jobs.enqueue_label_diff_after_commit", AsyncMock()),
    ):
        await save_source_content(
            project_id,
            SourceContentSave(
                content=BASE.decode() + "ex:HolySee a owl:Class .\n",
                commit_message="Update class Holy See",
                base_revision=initial.hash,
            ),
            _db(),
            SimpleNamespace(get=AsyncMock(return_value=project)),
            _Storage(),
            _Ontology(),
            git,
            SimpleNamespace(record_events_from_diff=AsyncMock(return_value=[])),
            SimpleNamespace(),
            USER,
            branch="main",
        )
    return git, project_id


@pytest.mark.usefixtures("no_preference")
async def test_direct_source_save_authors_with_noreply_alias(tmp_path: Path) -> None:
    git, project_id = await _source_save(tmp_path)
    head = git.get_history(project_id, branch="main", all_branches=False)[0]
    assert head.message.startswith("Update class Holy See")
    assert head.author_email == ALIAS


@pytest.mark.usefixtures("verified_preference")
async def test_direct_source_save_uses_verified_opt_in(tmp_path: Path) -> None:
    git, project_id = await _source_save(tmp_path)
    assert git.get_history(project_id, branch="main", all_branches=False)[0].author_email == (
        VERIFIED_EMAIL
    )


def _normalization(tmp_path: Path) -> tuple[NormalizationService, Any, BareGitRepositoryService]:
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    git.initialize_repository(project_id, BASE, "ontology.ttl")
    project = SimpleNamespace(
        id=project_id, user_role="owner", source_file_path="ontology.ttl", git_ontology_path=None
    )
    service = NormalizationService(_db(), _Storage(), git)
    report = SimpleNamespace(
        triple_count=1,
        prefixes_removed=[],
        prefixes_added=[],
        original_format="turtle",
        original_size_bytes=len(BASE),
        normalized_size_bytes=len(BASE) + 1,
        format_converted=False,
        to_dict=lambda: {},
    )
    service.extractor = SimpleNamespace(  # type: ignore[assignment]
        normalize_to_turtle=lambda content, _filename: (content + b"\n", report)
    )
    return service, project, git


@pytest.mark.usefixtures("no_preference")
async def test_normalization_by_user_authors_with_noreply_alias(tmp_path: Path) -> None:
    service, project, git = _normalization(tmp_path)
    await service.run_normalization(project, USER)  # type: ignore[arg-type]
    head = git.get_history(project.id, branch="main", all_branches=False)[0]
    assert head.message.startswith("Normalize ontology to canonical Turtle format")
    assert head.author_email == ALIAS


async def test_system_normalization_keeps_system_identity(tmp_path: Path) -> None:
    service, project, git = _normalization(tmp_path)
    await service.run_normalization(project, None)  # type: ignore[arg-type]
    head = git.get_history(project.id, branch="main", all_branches=False)[0]
    assert head.author_email == "system@ontokit.dev"


# ── Merge (interactive PR merge and suggestion approval) ─────────────────────


def _merge_service(
    *, project_lookup: bool = True
) -> tuple[PullRequestService, MagicMock, Any, Any]:
    project = MagicMock(id=uuid4(), pr_approval_required=0, github_integration=None)
    project.members = [MagicMock(user_id=USER.id, role="owner")]
    project.owner_id = USER.id
    pr = MagicMock(
        id=uuid4(),
        pr_number=1,
        status="open",
        source_branch="suggest/x",
        target_branch="main",
        title="Suggestion",
        author_id="someone-else",
        reviews=[],
        github_pr_number=None,
    )
    git = MagicMock()
    git.list_branches.return_value = []
    git.merge_branch.return_value = MagicMock(success=True, merge_commit_hash="abc")
    db = AsyncMock()
    db.add = MagicMock()
    project_result = MagicMock()
    project_result.scalar_one_or_none.return_value = project
    pr_result = MagicMock()
    pr_result.scalar_one_or_none.return_value = pr
    none_result = MagicMock()
    none_result.scalar_one_or_none.return_value = None
    none_result.scalars.return_value.all.return_value = []
    lookups = [project_result, pr_result] if project_lookup else [pr_result]
    db.execute.side_effect = [*lookups, *([none_result] * 20)]
    service = PullRequestService(
        db, git_service=git, github_service=MagicMock(), user_service=MagicMock()
    )
    return service, git, project, pr


@pytest.mark.usefixtures("no_preference")
async def test_pr_merge_commit_uses_noreply_alias() -> None:
    service, git, _project, _pr = _merge_service()

    await service.merge_pull_request(
        _project.id, 1, PRMergeRequest(merge_message="Merge it", delete_source_branch=False), USER
    )

    kwargs = git.merge_branch.call_args.kwargs
    assert (kwargs["author_name"], kwargs["author_email"]) == ("UAT D10 owner", ALIAS)


@pytest.mark.usefixtures("no_preference")
async def test_suggestion_approval_merge_uses_approvers_alias() -> None:
    # The reviewer check (patched) supplies the project; only the PR is looked up.
    service, git, project, pr = _merge_service(project_lookup=False)

    with patch(
        "ontokit.services.suggestion_service.SuggestionService._verify_reviewer_access",
        AsyncMock(return_value=project),
    ):
        await service._merge_pull_request_for_suggestion(
            project.id,
            pr.pr_number,
            PRMergeRequest(merge_message="Merge suggestion: s_abc", delete_source_branch=False),
            USER,
        )

    assert git.merge_branch.call_args.kwargs["author_email"] == ALIAS


# ── Project creation and GitHub import ───────────────────────────────────────


@pytest.mark.usefixtures("no_preference")
async def test_project_import_initial_commit_uses_alias() -> None:
    from ontokit.services.project_service import ProjectService
    from tests.unit.test_project_service import _make_simulate_refresh

    db = AsyncMock()
    db.add = MagicMock()
    db.refresh.side_effect = _make_simulate_refresh(USER.id, extended=True)
    git = MagicMock()
    git.initialize_repository.return_value = MagicMock(hash="abc")
    storage = AsyncMock()
    storage.upload_file = AsyncMock(return_value="projects/x/ontology.ttl")

    await ProjectService(db=db, git_service=git).create_from_import(
        file_content=BASE, filename="x.ttl", is_public=True, owner=USER, storage=storage
    )

    assert git.initialize_repository.call_args.kwargs["author_email"] == ALIAS


@pytest.mark.usefixtures("no_preference")
@pytest.mark.parametrize("clone_fails", [False, True], ids=["normalize-commit", "fallback-init"])
async def test_github_import_commits_use_alias(clone_fails: bool) -> None:
    from ontokit.services.project_service import ProjectService
    from tests.unit.test_project_service import _make_simulate_refresh

    db = AsyncMock()
    db.add = MagicMock()
    db.refresh.side_effect = _make_simulate_refresh(USER.id, extended=True)
    git = MagicMock()
    git.repository_exists.return_value = True
    git.initialize_repository.return_value = MagicMock(hash="abc")
    git.commit_changes.return_value = MagicMock(hash="def")
    git.clone_from_github = MagicMock(
        side_effect=Exception("clone failed") if clone_fails else None
    )
    storage = AsyncMock()
    storage.upload_file = AsyncMock(return_value="projects/x/ontology.ttl")

    await ProjectService(db=db, git_service=git).create_from_github(
        file_content=BASE,
        filename="ontology.ttl",
        repo_owner="o",
        repo_name="r",
        ontology_file_path="src/ontology.ttl",
        default_branch="main",
        is_public=True,
        owner=USER,
        storage=storage,
        github_token="t",
    )

    writer = git.initialize_repository if clone_fails else git.commit_changes
    assert writer.call_args.kwargs["author_email"] == ALIAS


# ── Regression guard ─────────────────────────────────────────────────────────

_AUTHOR_KEYWORDS = {"author_email", "committer_email"}
_ACCOUNT_EMAIL_ATTRIBUTES = {"email", "user_email", "author_email", "reviewer_email"}


def _account_email_sources(node: ast.AST) -> Iterator[ast.Attribute]:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in _ACCOUNT_EMAIL_ATTRIBUTES:
            yield child


def test_no_git_write_takes_an_account_email_attribute() -> None:
    """Fails if any call passes ``<something>.email`` as a git author/committer.

    The git layer's own read-back (``commit.author.email`` into CommitInfo)
    and DB records are not git writes and are not flagged.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "ontokit").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            callee = call.func.attr if isinstance(call.func, ast.Attribute) else ""
            if callee not in {
                "commit_changes",
                "merge_branch",
                "initialize_repository",
                "write_file",
            }:
                continue
            for keyword in call.keywords:
                if keyword.arg in _AUTHOR_KEYWORDS:
                    for source in _account_email_sources(keyword.value):
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{source.lineno} "
                            f"{keyword.arg}=...{ast.unparse(source)}"
                        )
    assert offenders == []


def test_guard_detects_a_real_email_author() -> None:
    sample = ast.parse("git.commit_changes(author_email=user.email)")
    call = sample.body[0].value  # type: ignore[attr-defined]
    assert [ast.unparse(a) for a in _account_email_sources(call.keywords[0].value)] == [
        "user.email"
    ]


@pytest.mark.usefixtures("no_preference")
async def test_no_commit_author_equals_the_account_email(tmp_path: Path) -> None:
    """Runtime guard: every commit these flows write avoids the account email."""
    git, project_id = await _source_save(tmp_path)
    service, project, norm_git = _normalization(tmp_path / "norm")
    await service.run_normalization(project, USER)  # type: ignore[arg-type]
    emails = {
        commit.author_email
        for repo, pid in ((git, project_id), (norm_git, project.id))
        for commit in repo.get_history(pid, branch="main", all_branches=False)
    }
    assert REAL_EMAIL not in emails
    assert ALIAS in emails
    assert emails <= {ALIAS, ONTOKIT_COMMITTER_EMAIL}
    await asyncio.sleep(0)
