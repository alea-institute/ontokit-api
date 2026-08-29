"""Real-PostgreSQL proof for generation-atomic demo publication."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ontokit.models.demo_generation import DemoGeneration, DemoGenerationStatus
from ontokit.models.ontology_index import IndexingStatus, OntologyIndexStatus
from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services.demo_project_provisioning import (
    DEMO_DEFAULT_BRANCH,
    ProvisionedDemo,
    build_demo_generation_key,
    ensure_demo_projects,
    fail_demo_generation,
    finalize_demo_publication,
    record_demo_preparation,
)


def _commits(first: str, second: str) -> dict[str, str]:
    return {
        "alea-institute/ontokit-demo-folio": first * 40,
        "alea-institute/ontokit-demo-semantic-canon": second * 40,
    }


async def _prepare(
    db: AsyncSession,
    provisioned: Sequence[ProvisionedDemo],
    commits: dict[str, str],
) -> None:
    for item in provisioned:
        commit = commits[item.destination_repository]
        db.add(
            OntologyIndexStatus(
                project_id=item.project_id,
                branch=DEMO_DEFAULT_BRANCH,
                status=IndexingStatus.READY.value,
                commit_hash=commit,
                entity_count=1,
            )
        )
        await db.commit()
        await record_demo_preparation(db, item, commit)


async def _public_demo_ids(db: AsyncSession) -> set[object]:
    result = await db.execute(
        select(Project.id).where(Project.is_demo.is_(True), Project.is_public.is_(True))
    )
    return set(result.scalars().all())


async def test_reader_observes_complete_old_or_new_generation_across_retry(
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
            is_demo=False,
            source_file_path=f"projects/source/{path}",
        )
        real_db_session.add(project)
        await real_db_session.flush()
        real_db_session.add(
            GitHubIntegration(
                project_id=project.id,
                repo_owner=owner,
                repo_name=repo,
                default_branch=source_branch,
                ontology_file_path=path,
                turtle_file_path=path,
                sync_enabled=True,
            )
        )
        sources.append(project)
    await real_db_session.commit()
    source_ids = [item.id for item in sources]
    generation_keys: list[str] = []

    try:
        old_commits = _commits("a", "b")
        old_key = build_demo_generation_key(old_commits)
        generation_keys.append(old_key)
        old = await ensure_demo_projects(real_db_session, old_key)
        await _prepare(real_db_session, old, old_commits)
        await finalize_demo_publication(real_db_session, old)
        old_ids = {item.project_id for item in old}
        assert await _public_demo_ids(real_db_session) == old_ids

        new_commits = _commits("c", "d")
        new_key = build_demo_generation_key(new_commits)
        generation_keys.append(new_key)
        failed = await ensure_demo_projects(real_db_session, new_key)
        new_ids = {item.project_id for item in failed}
        assert old_ids.isdisjoint(new_ids)
        await fail_demo_generation(
            real_db_session,
            failed,
            "repository_or_index_preparation_failed",
        )
        assert await _public_demo_ids(real_db_session) == old_ids

        retry = await ensure_demo_projects(real_db_session, new_key)
        assert {item.project_id for item in retry} == new_ids
        await _prepare(real_db_session, retry, new_commits)

        bind = real_db_session.bind
        assert bind is not None
        readers = async_sessionmaker(bind, expire_on_commit=False)
        async with readers() as reader:
            # This statement runs while the replacement is fully prepared but
            # hidden; the reader sees the complete old pair.
            assert await _public_demo_ids(reader) == old_ids

            await finalize_demo_publication(real_db_session, retry)

            # Publication is one database commit. The next reader statement
            # sees the complete new pair, never a one-old/one-new mixture.
            assert await _public_demo_ids(reader) == new_ids

        generations_result = await real_db_session.execute(
            select(DemoGeneration).where(DemoGeneration.generation_key.in_([old_key, new_key]))
        )
        generations = {
            generation.generation_key: generation
            for generation in generations_result.scalars().all()
        }
        assert generations[old_key].status == DemoGenerationStatus.RETIRED.value
        assert generations[new_key].status == DemoGenerationStatus.ACTIVE.value
        assert generations[new_key].attempt_count == 2
        assert generations[new_key].failure_count == 1
        assert generations[new_key].last_failure_reason == (
            "repository_or_index_preparation_failed"
        )

        # A successful retry is idempotent and preserves generation identity.
        active_retry = await ensure_demo_projects(real_db_session, new_key)
        assert all(item.already_active for item in active_retry)
        assert {item.project_id for item in active_retry} == new_ids
        await finalize_demo_publication(real_db_session, active_retry)
        assert await _public_demo_ids(real_db_session) == new_ids
    finally:
        await real_db_session.rollback()
        demo_result = await real_db_session.execute(
            select(Project.id).where(Project.demo_source_project_id.in_(source_ids))
        )
        demo_ids = list(demo_result.scalars().all())
        if demo_ids:
            await real_db_session.execute(delete(Project).where(Project.id.in_(demo_ids)))
            await real_db_session.commit()
        generation_result = await real_db_session.execute(
            select(DemoGeneration.id).where(DemoGeneration.generation_key.in_(generation_keys))
        )
        generation_ids = list(generation_result.scalars().all())
        if generation_ids:
            await real_db_session.execute(
                delete(DemoGeneration).where(DemoGeneration.id.in_(generation_ids))
            )
            await real_db_session.commit()
        await real_db_session.execute(delete(Project).where(Project.id.in_(source_ids)))
        await real_db_session.commit()
