"""Focused lifecycle proofs for generation-atomic demo provisioning."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from ontokit.models.demo_generation import DemoGeneration, DemoGenerationStatus
from ontokit.models.ontology_index import IndexingStatus, OntologyIndexStatus
from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services.demo_project_provisioning import (
    DEMO_DEFAULT_BRANCH,
    DemoProvisioningRefused,
    ProvisionedDemo,
    build_demo_generation_key,
    ensure_demo_projects,
    fail_demo_generation,
    finalize_demo_publication,
    record_demo_preparation,
)


class _Result:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any | None:
        assert len(self._rows) <= 1
        return self._rows[0] if self._rows else None


class _ProvisioningSession:
    """Deterministic AsyncSession double for generation lifecycle query order."""

    def __init__(self, sources: Sequence[GitHubIntegration]) -> None:
        self.sources = list(sources)
        self.generations: list[DemoGeneration] = []
        self.projects: list[Project] = []
        self.integrations: list[GitHubIntegration] = []
        self.indexes: list[OntologyIndexStatus] = []
        self._results: list[_Result] = []
        self.commit_count = 0
        self.rollback_count = 0

    def generation(self, generation_key: str) -> DemoGeneration | None:
        return next(
            (item for item in self.generations if item.generation_key == generation_key), None
        )

    def generation_projects(self, generation: DemoGeneration) -> list[Project]:
        return [item for item in self.projects if item.demo_generation_id == generation.id]

    def queue_ensure(self, generation_key: str) -> None:
        generation = self.generation(generation_key)
        candidates = self.generation_projects(generation) if generation is not None else []
        self._results = [
            _Result([self.sources[0]]),
            _Result([self.sources[1]]),
            _Result([generation] if generation is not None else []),
            _Result(candidates[:1]),
            _Result(candidates[1:2]),
        ]

    def queue_record(self, item: ProvisionedDemo, commit_hash: str) -> None:
        project = next(project for project in self.projects if project.id == item.project_id)
        status = OntologyIndexStatus(
            id=uuid.uuid4(),
            project_id=item.project_id,
            branch=DEMO_DEFAULT_BRANCH,
            status=IndexingStatus.READY.value,
            commit_hash=commit_hash,
        )
        self.indexes.append(status)
        self._results = [_Result([project]), _Result([status])]

    def queue_failure(self, generation: DemoGeneration) -> None:
        self._results = [_Result([generation])]

    def queue_finalize(
        self,
        generation: DemoGeneration,
        projects: Sequence[Project],
        *,
        active: Sequence[DemoGeneration],
        visible: Sequence[Project],
    ) -> None:
        source_projects = [source.project for source in self.sources]
        source_integrations = self.sources
        indexes = [
            index for index in self.indexes if index.project_id in {item.id for item in projects}
        ]
        self._results = [
            _Result(projects),
            _Result(source_projects),
            _Result([generation]),
        ]
        if generation.status != DemoGenerationStatus.ACTIVE.value:
            self._results.extend(
                [
                    _Result(source_integrations),
                    _Result(indexes),
                    _Result(active),
                    _Result(visible),
                ]
            )

    async def execute(self, _statement: object) -> _Result:
        assert self._results, "unexpected provisioning query"
        return self._results.pop(0)

    def add(self, value: object) -> None:
        if isinstance(value, DemoGeneration):
            self.generations.append(value)
        elif isinstance(value, Project) and value.is_demo:
            self.projects.append(value)
        elif isinstance(value, GitHubIntegration):
            self.integrations.append(value)

    async def flush(self) -> None:
        for generation in self.generations:
            if generation.id is None:
                generation.id = uuid.uuid4()
        for project in self.projects:
            if project.id is None:
                project.id = uuid.uuid4()

    async def commit(self) -> None:
        assert not self._results, "provisioning did not consume its expected queries"
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


def _source(owner: str, repo: str, branch: str, path: str) -> GitHubIntegration:
    project = Project(
        id=uuid.uuid4(),
        name=repo,
        owner_id="demo-owner",
        is_public=True,
        is_demo=False,
        source_file_path=path,
    )
    integration = GitHubIntegration(
        id=uuid.uuid4(),
        project_id=project.id,
        repo_owner=owner,
        repo_name=repo,
        default_branch=branch,
        ontology_file_path=path,
        turtle_file_path=path,
        sync_enabled=True,
    )
    integration.project = project
    return integration


def _commits(first: str, second: str) -> dict[str, str]:
    return {
        "alea-institute/ontokit-demo-folio": first * 40,
        "alea-institute/ontokit-demo-semantic-canon": second * 40,
    }


async def _prepare(
    session: _ProvisioningSession,
    provisioned: Sequence[ProvisionedDemo],
    commits: dict[str, str],
) -> None:
    for item in provisioned:
        commit = commits[item.destination_repository]
        session.queue_record(item, commit)
        await record_demo_preparation(session, item, commit)  # type: ignore[arg-type]


async def test_failed_preparation_preserves_active_generation_and_retry_is_idempotent() -> None:
    session = _ProvisioningSession(
        [
            _source("alea-institute", "FOLIO", "develop", "FOLIO.owl"),
            _source("CatholicOS", "ontology-semantic-canon", "release", "ontology.ttl"),
        ]
    )
    first_commits = _commits("a", "b")
    first_key = build_demo_generation_key(first_commits)
    session.queue_ensure(first_key)
    first = await ensure_demo_projects(session, first_key)  # type: ignore[arg-type]
    first_generation = session.generation(first_key)
    assert first_generation is not None
    assert all(item.created and not item.already_active for item in first)
    assert not any(project.is_public for project in session.projects)

    await _prepare(session, first, first_commits)
    first_projects = session.generation_projects(first_generation)
    session.queue_finalize(first_generation, first_projects, active=[], visible=[])
    await finalize_demo_publication(session, first)  # type: ignore[arg-type]
    assert first_generation.status == DemoGenerationStatus.ACTIVE.value
    assert all(project.is_public for project in first_projects)

    second_commits = _commits("c", "d")
    second_key = build_demo_generation_key(second_commits)
    session.queue_ensure(second_key)
    second = await ensure_demo_projects(session, second_key)  # type: ignore[arg-type]
    second_generation = session.generation(second_key)
    assert second_generation is not None
    second_ids = {item.project_id for item in second}
    assert all(project.is_public for project in first_projects)
    assert not any(project.is_public for project in session.generation_projects(second_generation))

    with pytest.raises(DemoProvisioningRefused, match="allowlisted safe label"):
        await fail_demo_generation(
            session,  # type: ignore[arg-type]
            second,
            "clone failed with token=secret",
        )
    session.queue_failure(second_generation)
    await fail_demo_generation(
        session,  # type: ignore[arg-type]
        second,
        "repository_or_index_preparation_failed",
    )
    assert second_generation.status == DemoGenerationStatus.FAILED.value
    assert second_generation.failure_count == 1
    assert all(project.is_public for project in first_projects)

    session.queue_ensure(second_key)
    retry = await ensure_demo_projects(session, second_key)  # type: ignore[arg-type]
    assert {item.project_id for item in retry} == second_ids
    assert not any(item.created or item.already_active for item in retry)
    assert second_generation.attempt_count == 2
    assert second_generation.failure_count == 1

    await _prepare(session, retry, second_commits)
    second_projects = session.generation_projects(second_generation)
    session.queue_finalize(
        second_generation,
        second_projects,
        active=[first_generation],
        visible=first_projects,
    )
    await finalize_demo_publication(session, retry)  # type: ignore[arg-type]

    # The single commit boundary switches both projects together; no project or
    # index identity is reused between the old and new generation.
    assert first_generation.status == DemoGenerationStatus.RETIRED.value
    assert second_generation.status == DemoGenerationStatus.ACTIVE.value
    assert not any(project.is_public for project in first_projects)
    assert all(project.is_public for project in second_projects)
    assert {project.id for project in first_projects}.isdisjoint(second_ids)
    assert {project.demo_commit_hash for project in second_projects} == set(second_commits.values())

    # Retrying finalization after an acknowledged success is a no-op.
    commits_before = session.commit_count
    session.queue_finalize(second_generation, second_projects, active=[], visible=[])
    await finalize_demo_publication(session, retry)  # type: ignore[arg-type]
    assert session.commit_count == commits_before
