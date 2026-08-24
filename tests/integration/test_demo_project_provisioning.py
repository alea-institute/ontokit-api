"""Real-PostgreSQL proof for idempotent demo project provisioning."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services.demo_project_provisioning import ensure_demo_projects


async def test_provisions_exactly_one_demo_per_source(
    real_db_session: AsyncSession,
) -> None:
    sources: list[Project] = []
    for name, owner, repo, path in (
        ("FOLIO", "alea-institute", "FOLIO", "FOLIO.owl"),
        (
            "Semantic Canon",
            "CatholicOS",
            "ontology-semantic-canon",
            "ontology.ttl",
        ),
    ):
        project = Project(
            name=name,
            owner_id="demo-test-owner",
            is_public=True,
            source_file_path=f"projects/source/{path}",
        )
        real_db_session.add(project)
        await real_db_session.flush()
        real_db_session.add(
            GitHubIntegration(
                project_id=project.id,
                repo_owner=owner,
                repo_name=repo,
                default_branch="main",
                ontology_file_path=path,
                turtle_file_path=path,
                sync_enabled=True,
            )
        )
        sources.append(project)
    await real_db_session.commit()
    source_ids = [item.id for item in sources]

    try:
        first = await ensure_demo_projects(real_db_session)
        second = await ensure_demo_projects(real_db_session)

        assert len(first) == len(second) == 2
        assert all(item.created for item in first)
        assert not any(item.created for item in second)
        assert {item.project_id for item in first} == {item.project_id for item in second}

        result = await real_db_session.execute(
            select(Project).where(Project.demo_source_project_id.in_(source_ids))
        )
        demos = list(result.scalars().all())
        assert len(demos) == 2
        assert all(project.is_demo and project.is_public for project in demos)
        assert all(project.source_file_path for project in demos)
    finally:
        await real_db_session.rollback()
        demo_result = await real_db_session.execute(
            select(Project.id).where(Project.demo_source_project_id.in_(source_ids))
        )
        demo_ids = list(demo_result.scalars().all())
        if demo_ids:
            await real_db_session.execute(delete(Project).where(Project.id.in_(demo_ids)))
            await real_db_session.commit()
        await real_db_session.execute(delete(Project).where(Project.id.in_(source_ids)))
        await real_db_session.commit()
