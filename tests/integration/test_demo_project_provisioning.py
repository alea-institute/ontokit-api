"""Real-PostgreSQL proof for idempotent demo project provisioning."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services.demo_project_provisioning import (
    DEMO_DEFAULT_BRANCH,
    ensure_demo_projects,
    finalize_demo_publication,
)


async def test_failed_first_activation_stays_private_until_retry_finishes(
    real_db_session: AsyncSession,
) -> None:
    sources: list[Project] = []
    for name, owner, repo, path, source_branch in (
        ("FOLIO", "alea-institute", "FOLIO", "FOLIO.owl", "develop"),
        (
            "Semantic Canon",
            "CatholicOS",
            "ontology-semantic-canon",
            "ontology.ttl",
            "release",
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
                # The demo target must follow its immutable mirror contract,
                # not an editable source-project branch preference.
                default_branch=source_branch,
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
        assert len(first) == 2
        assert all(item.created for item in first)

        result = await real_db_session.execute(
            select(Project).where(Project.demo_source_project_id.in_(source_ids))
        )
        demos = list(result.scalars().all())
        assert len(demos) == 2
        assert all(project.is_demo and not project.is_public for project in demos)
        assert all(project.source_file_path for project in demos)
        integration_result = await real_db_session.execute(
            select(GitHubIntegration).where(
                GitHubIntegration.project_id.in_([project.id for project in demos])
            )
        )
        demo_integrations = list(integration_result.scalars().all())
        assert len(demo_integrations) == 2
        assert all(
            integration.default_branch == DEMO_DEFAULT_BRANCH for integration in demo_integrations
        )

        discoverable = await real_db_session.scalar(
            select(Project.id).where(Project.is_demo.is_(True), Project.is_public.is_(True))
        )
        assert discoverable is None

        # Simulate a failed first preparation: ensure committed the private
        # identities, but the resync entrypoint never finalized publication.
        retry = await ensure_demo_projects(real_db_session)
        assert not any(item.created for item in retry)
        assert {item.project_id for item in first} == {item.project_id for item in retry}
        assert not any(project.is_public for project in demos)

        await finalize_demo_publication(real_db_session, retry)
        real_db_session.expire_all()
        published_result = await real_db_session.execute(
            select(Project).where(Project.demo_source_project_id.in_(source_ids))
        )
        published = list(published_result.scalars().all())
        assert all(project.is_public for project in published)

        # A later refresh starts from the already-published rows. If repository
        # preparation then fails before finalization, they must stay public and
        # continue serving the previously known-good repository/index pair.
        refresh = await ensure_demo_projects(real_db_session)
        assert not any(item.created for item in refresh)
        assert all(project.is_public for project in published)
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
