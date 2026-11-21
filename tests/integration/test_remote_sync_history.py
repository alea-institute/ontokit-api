"""Remote sync against a local bare Git remote standing in for GitHub.

PostgreSQL, Git, the sync service, and the change-history reconciler are real.
The remote is a bare repository on disk, so nothing reaches the network. Only
the mirror credential lookup is replaced, with a token the file transport
ignores.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pygit2
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ontokit import worker
from ontokit.api.utils.redis import close_arq_pool
from ontokit.core.config import settings
from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services import mirror_credential
from ontokit.services.change_event_service import ChangeEventService
from ontokit.services.change_history_reconciler import ReconciliationResult
from ontokit.services.demo_target_authorizer import integration_target_load

pytestmark = pytest.mark.integration

NAMESPACE = "https://example.org/agreements#"
CONTRACT = NAMESPACE + "Contract"
TREATY = NAMESPACE + "Treaty"
ACCORD = NAMESPACE + "Accord"
PREFIXES = f"""\
@prefix ex: <{NAMESPACE}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

"""
# Blank lines keep each class its own merge hunk.
AGREEMENT = 'ex:Agreement a owl:Class ; rdfs:label "Agreement"@en .\n\n'
TREATY_BLOCK = 'ex:Treaty a owl:Class ; rdfs:subClassOf ex:Agreement ; rdfs:label "Treaty"@en .\n\n'
ACCORD_BLOCK = 'ex:Accord a owl:Class ; rdfs:subClassOf ex:Agreement ; rdfs:label "Accord"@en .\n'


def ontology(contract_label: str, *, treaty: bool = True, accord: bool = False) -> bytes:
    contract = (
        "ex:Contract a owl:Class ; rdfs:subClassOf ex:Agreement ;\n"
        f'    rdfs:label "{contract_label}"@en .\n\n'
    )
    text = PREFIXES + AGREEMENT + contract + (TREATY_BLOCK if treaty else "")
    return (text + "\n\n\n" + (ACCORD_BLOCK if accord else "")).encode()


def write(
    repo: pygit2.Repository, content: bytes, parent: pygit2.Oid | None, when: datetime
) -> pygit2.Oid:
    """Commit ``content`` as an upstream editor would, then point main at it."""
    tree = repo.TreeBuilder()
    tree.insert("ontology.ttl", repo.create_blob(content), pygit2.GIT_FILEMODE_BLOB)
    signature = pygit2.Signature("Upstream Editor", "upstream@example.org", int(when.timestamp()))
    oid = repo.create_commit(
        None, signature, signature, "Upstream edit", tree.write(), [parent] if parent else []
    )
    # A plain reference update, so it can also rewrite history like a force push.
    repo.references.create("refs/heads/main", oid, force=True)
    return oid


@pytest_asyncio.fixture
async def project(
    real_db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[UUID]:
    monkeypatch.setattr(settings, "git_repos_base_path", str(tmp_path / "repos"))

    async def local_token(*_arguments: object) -> str:
        return "local-remote-token"

    monkeypatch.setattr(mirror_credential, "resolve_mirror_credential", local_token)
    project_id = uuid4()
    real_db_session.add_all(
        [
            Project(
                id=project_id,
                name="Synced agreements",
                owner_id="owner",
                source_file_path="ontology.ttl",
            ),
            GitHubIntegration(
                project_id=project_id,
                repo_owner="example-org",
                repo_name="agreements",
                default_branch="main",
                ontology_file_path="ontology.ttl",
                sync_enabled=True,
            ),
        ]
    )
    await real_db_session.commit()
    try:
        yield project_id
    finally:
        await close_arq_pool()
        await real_db_session.rollback()
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()


async def _sync(db: AsyncSession, project_id: UUID) -> dict[str, object]:
    await db.rollback()
    integration = await db.scalar(
        select(GitHubIntegration)
        .options(integration_target_load())
        .where(GitHubIntegration.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    assert integration is not None
    outcome = await worker.sync_github_integration(db, integration, BareGitRepositoryService())
    assert outcome == worker.GITHUB_SYNC_SYNCED
    return {"status": integration.sync_status, "error": integration.sync_error}


async def _reconcile(db: AsyncSession, project_id: UUID) -> ReconciliationResult:
    context = {"session_factory": async_sessionmaker(db.bind, expire_on_commit=False)}
    return await worker.reconcile_change_history_task(context, str(project_id), "main")


def _summary(item: object) -> tuple[object, ...]:
    data = item.model_dump()  # type: ignore[attr-defined]
    return (
        data["net_change"],
        data["origins"],
        [author["user_id"] for author in data["authors"]],
        [(event["event_type"], event["changed_fields"]) for event in data["events"]],
    )


@pytest.mark.asyncio
async def test_sync_records_exact_history_for_rewrites_and_a_force_pushed_upstream(
    real_db_session: AsyncSession,
    project: UUID,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    git = BareGitRepositoryService()
    local = git.get_repository(project).repo
    # The imported ontology predates the one-day feed window.
    initial = write(local, ontology("Contract"), None, now - timedelta(days=10))
    local.references.create("HEAD", "refs/heads/main", force=True)
    upstream = pygit2.init_repository(str(tmp_path / "upstream.git"), bare=True)
    git.get_repository(project).add_remote("origin", str(tmp_path / "upstream.git"))

    # The first sync publishes the canonical history to the empty remote.
    assert await _sync(real_db_session, project) == {"status": "idle", "error": None}
    assert upstream.references["refs/heads/main"].target == initial
    baseline = await _reconcile(real_db_session, project)
    assert (baseline["processed"], baseline["recorded"]) == (1, 3)

    # Upstream renames Contract twice between syncs.
    first = write(upstream, ontology("Binding agreement"), initial, now - timedelta(minutes=20))
    second = write(upstream, ontology("Binding contract"), first, now - timedelta(minutes=10))

    # By default the mirror is outbound-only: the canonical history stays put.
    diverged = await _sync(real_db_session, project)
    assert diverged["status"] == "diverged"
    assert local.references["refs/heads/main"].target == initial
    assert (await _reconcile(real_db_session, project))["processed"] == 0

    monkeypatch.setattr(settings, "github_mirror_outbound_only", False)
    assert await _sync(real_db_session, project) == {"status": "idle", "error": None}
    assert local.references["refs/heads/main"].target == second
    pulled = await _reconcile(real_db_session, project)
    assert (pulled["processed"], pulled["recorded"], pulled["more"]) == (2, 2, False)

    feed = await ChangeEventService(real_db_session).list_changes(project, branch="main", days=1)
    assert [item.entity_iri for item in feed.items] == [CONTRACT]
    (contract,) = feed.items
    assert _summary(contract) == (
        "modified",
        ["system"],
        ["system"],
        [("rename", ["labels"]), ("rename", ["labels"])],
    )
    assert [event.commit_hash for event in contract.events] == [str(second), str(first)]
    assert [change.model_dump() for change in contract.field_changes] == [
        {"field": "labels", "before": ["Contract"], "after": ["Binding contract"]}
    ]

    # Upstream force-pushes a history that drops both renames, removes Treaty,
    # and adds Accord. The canonical branch keeps its renames and merges the rest.
    rewritten = write(
        upstream,
        ontology("Contract", treaty=False, accord=True),
        initial,
        now - timedelta(minutes=5),
    )
    assert not upstream.descendant_of(rewritten, second)
    assert await _sync(real_db_session, project) == {"status": "idle", "error": None}
    merge = local.references["refs/heads/main"].peel(pygit2.Commit)
    assert list(merge.parent_ids) == [second, rewritten]
    assert upstream.references["refs/heads/main"].target == merge.id
    merged = await _reconcile(real_db_session, project)
    assert (merged["processed"], merged["recorded"], merged["more"]) == (1, 2, False)

    feed = await ChangeEventService(real_db_session).list_changes(project, branch="main", days=1)
    items = {item.entity_iri: item for item in feed.items}
    assert set(items) == {CONTRACT, TREATY, ACCORD}
    assert _summary(items[CONTRACT]) == _summary(contract)
    assert _summary(items[TREATY]) == ("removed", ["system"], ["system"], [("delete", [])])
    assert _summary(items[ACCORD]) == ("added", ["system"], ["system"], [("create", [])])
    for iri in (TREATY, ACCORD):
        (event,) = items[iri].events
        assert event.commit_hash == str(merge.id)
    assert feed.summary.by_net_change == {
        "added": 1,
        "modified": 1,
        "removed": 1,
        "transient": 0,
    }
    assert feed.summary.by_origin == {"human": 0, "ai": 0, "ai_edited": 0, "system": 4}

    # Nothing is left to record, and syncing again changes nothing.
    assert await _sync(real_db_session, project) == {"status": "idle", "error": None}
    assert (await _reconcile(real_db_session, project))["processed"] == 0
