"""Focused lifecycle proofs for resettable demo database provisioning."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services.demo_project_provisioning import (
    DEMO_DEFAULT_BRANCH,
    ensure_demo_projects,
    finalize_demo_publication,
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
    """Small deterministic AsyncSession double for the provisioning query order."""

    def __init__(self, sources: Sequence[GitHubIntegration]) -> None:
        self.sources = list(sources)
        self.demos: list[Project] = []
        self.integrations: list[GitHubIntegration] = []
        self._results: list[_Result] = []
        self.commit_count = 0

    def queue_ensure(self, *, existing: bool) -> None:
        self._results = []
        for index, source in enumerate(self.sources):
            demo = self.demos[index] if existing else None
            integration = self.integrations[index] if existing else None
            self._results.extend(
                [
                    _Result([source]),
                    _Result([integration] if integration is not None else []),
                    _Result([demo] if demo is not None else []),
                ]
            )

    def queue_finalize(self) -> None:
        self._results = [_Result(self.demos), _Result([*self.sources, *self.integrations])]

    async def execute(self, _statement: object) -> _Result:
        assert self._results, "unexpected provisioning query"
        return self._results.pop(0)

    def add(self, value: object) -> None:
        if isinstance(value, Project) and value.is_demo:
            self.demos.append(value)
        elif isinstance(value, GitHubIntegration):
            self.integrations.append(value)

    async def flush(self) -> None:
        for project in self.demos:
            if project.id is None:
                project.id = uuid.uuid4()

    async def commit(self) -> None:
        assert not self._results, "provisioning did not consume its expected queries"
        self.commit_count += 1


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


async def test_first_activation_stays_private_until_complete_retry() -> None:
    session = _ProvisioningSession(
        [
            _source("alea-institute", "FOLIO", "develop", "FOLIO.owl"),
            _source(
                "CatholicOS",
                "ontology-semantic-canon",
                "release",
                "ontology.ttl",
            ),
        ]
    )

    session.queue_ensure(existing=False)
    first = await ensure_demo_projects(session)  # type: ignore[arg-type]
    assert all(item.created for item in first)
    assert all(project.is_demo and not project.is_public for project in session.demos)
    assert all(
        integration.default_branch == DEMO_DEFAULT_BRANCH for integration in session.integrations
    )

    # A failed clone/index run never calls publication. Its retry reuses the
    # private identities rather than exposing incomplete projects or duplicating them.
    session.queue_ensure(existing=True)
    retry = await ensure_demo_projects(session)  # type: ignore[arg-type]
    assert not any(item.created for item in retry)
    assert {item.project_id for item in retry} == {item.project_id for item in first}
    assert not any(project.is_public for project in session.demos)

    session.queue_finalize()
    await finalize_demo_publication(session, retry)  # type: ignore[arg-type]
    assert all(project.is_public for project in session.demos)

    # A later failed refresh leaves already-published demos available with the
    # known-good repository/index pair restored by the resync boundary.
    session.queue_ensure(existing=True)
    await ensure_demo_projects(session)  # type: ignore[arg-type]
    assert all(project.is_public for project in session.demos)
    assert session.commit_count == 4
