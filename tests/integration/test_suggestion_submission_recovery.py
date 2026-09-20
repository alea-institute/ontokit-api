"""HTTP recovery with real draft, Git, allowance and paid-call persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ontokit.api.routes import suggestions
from ontokit.core.auth import CurrentUser, get_current_user
from ontokit.core.database import get_db
from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.embedding import EmbeddingJob, EntityEmbedding, ProjectEmbeddingConfig
from ontokit.models.llm_config import LLMAuditLog
from ontokit.models.notification import Notification
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import PullRequest
from ontokit.models.suggestion_session import SuggestionSession
from ontokit.services.llm.pricing import PricingUnavailableError
from ontokit.services.pull_request_service import PullRequestService
from ontokit.services.suggestion_service import SuggestionService
from ontokit.services.trust_rate_limiter import submission_key

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["suggester", "editor"])
@pytest.mark.parametrize("failure", ["budget", "pricing"])
@pytest.mark.parametrize("prior_paid_call", [False, True])
async def test_http_submission_refusal_preserves_draft_and_fresh_retry(
    real_db_session: AsyncSession,
    real_redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    prior_paid_call: bool,
    role: str,
) -> None:
    """A historical untrusted draft can recover without undoing completed spend.

    Both labels cost one token, so which RDF entity is visited first is irrelevant.
    Provider/pricing and queue transport are isolated; validation, accounting,
    locks, PR allocation/binding, notifications and allowance consumption are real.
    """
    factory = async_sessionmaker(real_db_session.bind, expire_on_commit=False)
    project_id = uuid4()
    user = CurrentUser(id=f"recovery-{project_id}", name="Recovery Contributor")
    owner_id = f"owner-{project_id}"
    project = Project(
        id=project_id,
        name="Submission recovery",
        owner_id=owner_id,
        source_file_path="ontology.ttl",
    )
    project.members.extend(
        [
            ProjectMember(user_id=owner_id, role="owner"),
            ProjectMember(user_id=user.id, role=role, is_trusted=False),
        ]
    )
    draft = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        user_name=user.name,
        session_id="historical-draft",
        branch="suggestion/historical-draft",
        beacon_token="synthetic-test-token",
        changes_count=2,
        revision=3,
        reviewer_feedback="Keep this saved review context",
        verification_passed=False,
    )
    config = ProjectEmbeddingConfig(
        project_id=project_id,
        provider="openai",
        model_name="synthetic-embedding",
        dimensions=3,
        monthly_budget_usd=(0.015 if prior_paid_call else 0.0) if failure == "budget" else 10.0,
    )
    real_db_session.add(project)
    await real_db_session.flush()
    real_db_session.add_all(
        [
            draft,
            config,
            EntityEmbedding(
                project_id=project_id,
                branch="main",
                entity_iri="https://example.test/Existing",
                entity_type="class",
                label="Existing",
                embedding_text="Existing",
                embedding=[0.0, 1.0, 0.0],
                dimensions=3,
                provider="openai",
                model_name="synthetic-embedding",
            ),
        ]
    )
    await real_db_session.commit()
    draft_id = draft.id
    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = (
        b"@prefix ex: <https://example.test/> .\n"
        b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        b"@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    )
    git.initialize_repository(project_id, initial, "ontology.ttl")
    git.create_branch(project_id, draft.branch, from_ref="main")
    content = initial + b'ex:A a owl:Class ; rdfs:label "Alfa" .\n'
    if prior_paid_call:
        content += b'ex:B a owl:Class ; rdfs:label "Beta" .\n'
    repo = git.get_repository(project_id)
    saved = repo.write_file(draft.branch, "ontology.ttl", content, "Saved historical draft")

    def head() -> str:
        return str(repo.repo.references[f"refs/heads/{draft.branch}"].target)

    assert head() == saved.hash

    provider = SimpleNamespace(
        provider_name="openai",
        model_id="synthetic-embedding",
        dimensions=3,
        embed_text=AsyncMock(return_value=[1.0, 0.0, 0.0]),
    )
    pricing = AsyncMock(return_value=(0.01, 0.0))
    if failure == "pricing":
        refusal = PricingUnavailableError("private pricing backend detail")
        pricing.side_effect = [(0.01, 0.0), refusal] if prior_paid_call else refusal
    monkeypatch.setattr("ontokit.services.embedding_service.get_model_pricing", pricing)
    monkeypatch.setattr(
        "ontokit.services.embedding_service.get_embedding_provider", lambda *_args: provider
    )
    monkeypatch.setattr("ontokit.core.database.async_session_maker", factory)
    verification = SimpleNamespace(enabled=True, verify=AsyncMock(return_value=True))
    monkeypatch.setattr(
        "ontokit.services.suggestion_service.get_verification_provider", lambda: verification
    )
    # The HTTP limiter uses real Redis; the background queue transport does not run workers.
    monkeypatch.setattr(suggestions, "get_arq_pool", AsyncMock(return_value=real_redis))
    queue = SimpleNamespace(enqueue_job=AsyncMock(return_value=None))
    monkeypatch.setattr("ontokit.api.utils.redis.get_arq_pool", AsyncMock(return_value=queue))
    monkeypatch.setattr(
        "ontokit.services.suggestion_service.get_pull_request_service",
        lambda db: PullRequestService(db, git_service=git),
    )
    requests: list[AsyncSession] = []
    closed: list[AsyncSession] = []

    async def request_db() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            requests.append(db)
            try:
                yield db
            finally:
                await db.close()
                closed.append(db)

    def service(db: Annotated[AsyncSession, Depends(get_db)]) -> SuggestionService:
        return SuggestionService(db, git)

    app = FastAPI()
    app.include_router(suggestions.router)
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[suggestions.get_service] = service
    path = f"/{project_id}/suggestions/sessions/{draft.session_id}/submit"
    allowance_key = submission_key(str(project_id), user.id)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(path, json={"summary": "Ready for review"})
            assert response.status_code == (402 if failure == "budget" else 503)
            assert response.json() == {
                "detail": "budget_exhausted"
                if failure == "budget"
                else "Embedding pricing is unavailable; duplicate check is paused."
            }
            assert "retry-after" not in response.headers
            assert requests == closed and len(requests) == 1
            assert await real_redis.get(allowance_key) is None
            queue.enqueue_job.assert_not_awaited()
            assert verification.verify.await_count == (1 if role == "suggester" else 0)
            assert head() == saved.hash
            assert repo.read_file(draft.branch, "ontology.ttl") == content
            async with factory() as check:
                persisted = await check.get(SuggestionSession, draft_id)
                assert persisted is not None
                assert (
                    persisted.status,
                    persisted.revision,
                    persisted.reviewer_feedback,
                    persisted.pr_id,
                    persisted.pr_number,
                    persisted.verification_passed,
                ) == (
                    "active",
                    3,
                    "Keep this saved review context",
                    None,
                    None,
                    False,
                )
                for model in (PullRequest, Notification, EmbeddingJob):
                    assert not (
                        await check.scalars(select(model).where(model.project_id == project_id))
                    ).all()
                receipts = (
                    await check.scalars(
                        select(LLMAuditLog).where(LLMAuditLog.project_id == project_id)
                    )
                ).all()
                assert len(receipts) == int(prior_paid_call)
                receipt_ids = {receipt.id for receipt in receipts}
                for receipt in receipts:
                    assert receipt.endpoint == "embeddings/duplicate-check"
                    assert receipt.user_id == user.id
                    assert receipt.input_tokens == 1
                    assert receipt.cost_estimate_usd == pytest.approx(0.01)
                    assert not receipt.is_byo_key
            assert provider.embed_text.await_count == int(prior_paid_call)

            # Restore readiness outside the failed request; retry through a new session.
            async with factory() as admin:
                ready = await admin.scalar(
                    select(ProjectEmbeddingConfig).where(
                        ProjectEmbeddingConfig.project_id == project_id
                    )
                )
                ready.monthly_budget_usd = 10.0
                await admin.commit()
            pricing.side_effect = None
            retry = await client.post(path, json={"summary": "Ready for review"})
            assert retry.status_code == 200, retry.text
            assert retry.json() == {"pr_number": 1, "pr_url": None, "status": "submitted"}
            assert len(requests) == 2 and requests[0] is not requests[1]
            assert closed == requests
            assert await real_redis.get(allowance_key) == (b"1" if role == "suggester" else None)
            assert verification.verify.await_count == (2 if role == "suggester" else 0)
            assert head() == saved.hash
            assert repo.read_file(draft.branch, "ontology.ttl") == content
            assert queue.enqueue_job.await_count == 2
            assert [c.args[0] for c in queue.enqueue_job.await_args_list] == [
                "run_ontology_index_task",
                "run_embedding_generation_task",
            ]
            async with factory() as check:
                persisted = await check.get(SuggestionSession, draft_id)
                prs = (
                    await check.scalars(
                        select(PullRequest).where(PullRequest.project_id == project_id)
                    )
                ).all()
                assert len(prs) == 1
                assert persisted.status == "submitted" and persisted.pr_id == prs[0].id
                assert persisted.pr_number == prs[0].pr_number == 1
                assert persisted.verification_passed is (role == "suggester")
                assert persisted.revision == 3
                assert persisted.reviewer_feedback == "Keep this saved review context"
                notifications = (
                    await check.scalars(
                        select(Notification).where(Notification.project_id == project_id)
                    )
                ).all()
                expected_types = (
                    ["suggestion_submitted"]
                    if role == "suggester"
                    else ["pr_opened", "suggestion_submitted"]
                )
                assert sorted((n.type, n.user_id) for n in notifications) == [
                    (t, owner_id) for t in expected_types
                ]
                jobs = (
                    await check.scalars(
                        select(EmbeddingJob).where(EmbeddingJob.project_id == project_id)
                    )
                ).all()
                assert len(jobs) == 1 and jobs[0].status == "pending"
                after = (
                    await check.scalars(
                        select(LLMAuditLog).where(LLMAuditLog.project_id == project_id)
                    )
                ).all()
                assert receipt_ids <= {receipt.id for receipt in after}
                assert len(after) == int(prior_paid_call) + (2 if prior_paid_call else 1)
                assert all(r.endpoint == "embeddings/duplicate-check" for r in after)
    finally:
        await real_db_session.rollback()
        async with factory() as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.commit()
        await real_redis.delete(allowance_key)
