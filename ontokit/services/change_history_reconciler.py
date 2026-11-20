"""Reconcile entity change events against a branch's first-parent history."""

import json
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import UUID

import pygit2
from arq import ArqRedis
from rdflib import Graph
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ontokit.git.bare_repository import BareGitRepositoryService, BareOntologyRepository
from ontokit.models.change_event import ChangeOrigin, EntityChangeEvent
from ontokit.models.change_history_watermark import ChangeHistoryWatermark
from ontokit.models.project import Project, get_git_ontology_path
from ontokit.models.pull_request import PullRequest
from ontokit.models.user_commit_identity import UserCommitIdentity
from ontokit.services.ai_origin_service import AIOriginService
from ontokit.services.change_event_service import ChangeEventService
from ontokit.services.commit_identity import noreply_alias
from ontokit.services.ontology import rdf_format_for_filename

logger = logging.getLogger(__name__)

RECONCILE_CHANGE_HISTORY_TASK = "reconcile_change_history_task"
_BACKFILL_DAYS = 90


class ReconciliationResult(TypedDict):
    processed: int
    recorded: int
    skipped: int
    parse_errors: int
    more: bool


async def enqueue_change_history(
    project_id: UUID, branch: str, *, pool: ArqRedis | None = None
) -> None:
    """Queue one job per branch, without affecting the landing operation."""
    from ontokit.api.utils.redis import get_arq_pool

    try:
        if pool is None:
            pool = await get_arq_pool()
        await pool.enqueue_job(
            RECONCILE_CHANGE_HISTORY_TASK,
            str(project_id),
            branch,
            _job_id=f"change-history:{project_id}:{branch}",
        )
    except Exception as exc:
        logger.warning(
            "Failed to queue change history for project %s branch %s (%s)",
            project_id,
            branch,
            type(exc).__name__,
        )


async def advance_inline_watermark(
    db: AsyncSession,
    project_id: UUID,
    branch: str,
    previous_head: str,
    commit_hash: str,
    *,
    is_default_branch: bool,
) -> None:
    """Advance a contiguous inline save under the source writer's branch lock."""
    watermark = ChangeHistoryWatermark
    if is_default_branch and await db.get(watermark, (project_id, branch)) is None:
        # Let the first reconciliation establish the historical baseline.
        return

    statement = insert(watermark).values(
        project_id=project_id, branch=branch, commit_hash=commit_hash
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[watermark.project_id, watermark.branch],
            set_={"commit_hash": statement.excluded.commit_hash, "updated_at": func.now()},
            where=watermark.commit_hash == previous_head,
        )
    )


class ChangeHistoryReconciler:
    def __init__(self, db: AsyncSession, git: BareGitRepositoryService) -> None:
        self._db = db
        self._git = git

    async def reconcile_branch(
        self, project_id: UUID, branch: str, *, max_commits: int = 50
    ) -> ReconciliationResult:
        result = ReconciliationResult(
            processed=0, recorded=0, skipped=0, parse_errors=0, more=False
        )
        async with self._db.begin():
            # Share the source writer's key so an inline save cannot race deduplication.
            locked = await self._db.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"suggestion:{project_id}:{branch}"},
            )
            if not locked or not self._git.repository_exists(project_id):
                return result

            project = await self._db.get(
                Project,
                project_id,
                options=[selectinload(Project.github_integration), selectinload(Project.members)],
            )
            if project is None:
                return result
            repository = self._git.get_repository(project_id)
            try:
                head = repository.repo.references[f"refs/heads/{branch}"].peel(pygit2.Commit)
            except KeyError:
                return result
            watermark = await self._db.get(ChangeHistoryWatermark, (project_id, branch))
            head_hash = str(head.id)
            if watermark is not None and watermark.commit_hash == head_hash:
                return result

            def advance(commit_hash: str) -> None:
                nonlocal watermark
                if watermark is None:
                    watermark = ChangeHistoryWatermark(
                        project_id=project_id, branch=branch, commit_hash=commit_hash
                    )
                    self._db.add(watermark)
                else:
                    watermark.commit_hash = commit_hash
                    watermark.updated_at = datetime.now(UTC)

            if branch != repository.get_default_branch() and (
                watermark is None or branch.startswith("suggest/")
            ):
                advance(head_hash)
                return result

            baseline, pending = _select_history(
                repository.repo, head, watermark.commit_hash if watermark else None, max_commits
            )
            if watermark is not None and baseline is None:
                logger.warning(
                    "Change history watermark %s is no longer an ancestor of %s; resetting",
                    watermark.commit_hash,
                    head_hash,
                )
                advance(head_hash)
                return result

            if not pending:
                advance(head_hash)
                return result

            filename = get_git_ontology_path(project)
            original_filename = (
                project.github_integration.ontology_file_path
                if project.github_integration
                else None
            )
            old_graph = None
            # A previous batch may have ended on a parse error. Recover the last
            # good graph while leaving those commits marked as processed.
            while baseline is not None:
                old_graph = _read_graph(repository, str(baseline.id), filename, original_filename)
                if old_graph is not None:
                    break
                baseline = baseline.parents[0] if baseline.parent_ids else None

            recorder = ChangeEventService(self._db)
            resolve_origin = AIOriginService(self._db).resolve
            for _ in range(min(max_commits, len(pending))):
                commit = pending.pop()
                commit_hash = str(commit.id)
                new_graph = _read_graph(repository, commit_hash, filename, original_filename)
                if new_graph is None:
                    logger.warning("Could not parse ontology at commit %s", commit_hash)
                    result["parse_errors"] += 1
                else:
                    existing = await self._db.scalar(
                        select(EntityChangeEvent.id)
                        .where(
                            EntityChangeEvent.project_id == project_id,
                            EntityChangeEvent.branch == branch,
                            EntityChangeEvent.commit_hash == commit_hash,
                        )
                        .limit(1)
                    )
                    if existing is None:
                        user_id, user_name, origin = await self._attribution(project, commit)
                        events = await recorder.record_events_from_diff(
                            project_id,
                            branch,
                            old_graph,
                            new_graph,
                            user_id,
                            user_name,
                            commit_hash,
                            occurred_at=datetime.fromtimestamp(commit.commit_time, UTC),
                            origin_resolver=resolve_origin,
                            default_origin=origin,
                            auto_commit=False,
                        )
                        result["recorded"] += len(events)
                    else:
                        result["skipped"] += 1
                    old_graph = new_graph
                advance(commit_hash)
                result["processed"] += 1
            # A landing while this job is running may deduplicate against its ID.
            current_ref = repository.repo.references.get(f"refs/heads/{branch}")
            result["more"] = bool(pending) or (
                current_ref is not None and current_ref.target != head.id
            )
        return result

    async def _attribution(
        self, project: Project, commit: pygit2.Commit
    ) -> tuple[str, str | None, ChangeOrigin]:
        # Synthetic PRs created from old Git history stored an email as the ID.
        pr = await self._db.scalar(
            select(PullRequest)
            .where(
                PullRequest.project_id == project.id,
                PullRequest.merge_commit_hash == str(commit.id),
                PullRequest.author_id.not_like("%@%"),
            )
            .order_by(PullRequest.pr_number.desc())
            .limit(1)
        )
        if pr is not None:
            return pr.author_id, _display_name(pr.author_name), ChangeOrigin.HUMAN

        author = commit.author
        name = _display_name(author.name)
        user_ids = {project.owner_id, *(member.user_id for member in project.members)}
        for user_id in user_ids:
            if user_id and noreply_alias(user_id, author.name) == author.email:
                return user_id, name, ChangeOrigin.HUMAN

        verified_ids = (
            await self._db.scalars(
                select(UserCommitIdentity.user_id)
                .where(
                    UserCommitIdentity.commit_email == author.email,
                    UserCommitIdentity.commit_email_verified.is_(True),
                    UserCommitIdentity.use_verified_email.is_(True),
                )
                .limit(2)
            )
        ).all()
        if len(verified_ids) == 1:
            return verified_ids[0], name, ChangeOrigin.HUMAN
        return "system", name, ChangeOrigin.SYSTEM


def _select_history(
    repo: pygit2.Repository, head: pygit2.Commit, previous_hash: str | None, max_commits: int
) -> tuple[pygit2.Commit | None, deque[pygit2.Commit]]:
    # Keep the oldest pending batch and one lookahead while finding its baseline.
    pending: deque[pygit2.Commit] = deque(maxlen=max_commits + 1)
    cutoff = (datetime.now(UTC) - timedelta(days=_BACKFILL_DAYS)).timestamp()
    walk = repo.walk(head.id, pygit2.enums.SortMode.TOPOLOGICAL)
    walk.simplify_first_parent()
    for commit in walk:
        if str(commit.id) == previous_hash if previous_hash else commit.commit_time < cutoff:
            return commit, pending
        pending.append(commit)

    if previous_hash is not None:
        previous = repo.get(previous_hash)
        if isinstance(previous, pygit2.Commit) and repo.descendant_of(head.id, previous.id):
            # A fast-forward can bring the old head in through a second parent.
            # Diff the landing merge against the last state known on this branch.
            pending.clear()
            walk = repo.walk(head.id, pygit2.enums.SortMode.TOPOLOGICAL)
            walk.simplify_first_parent()
            for commit in walk:
                if not repo.descendant_of(commit.id, previous.id):
                    break
                pending.append(commit)
            return previous, pending
    return None, pending


def _display_name(name: str | None) -> str | None:
    # Git author names are untrusted and sometimes contain the email itself.
    return name[:255] if name and "@" not in name else None


def _read_graph(
    repository: BareOntologyRepository,
    commit_hash: str,
    filename: str,
    original_filename: str | None,
) -> Graph | None:
    try:
        try:
            content = repository.read_file(commit_hash, filename)
        except KeyError:
            if not original_filename or original_filename == filename:
                raise
            filename = original_filename
            content = repository.read_file(commit_hash, filename)
        rdf_format = rdf_format_for_filename(filename)
        if rdf_format == "json-ld" and _has_external_context(json.loads(content)):
            # A background parse of Git history must not fetch URLs or local files.
            return None
        return Graph().parse(data=content, format=rdf_format)
    except Exception:
        # Parser exceptions can contain snippets of the ontology.
        return None


def _has_external_context(value: object, *, in_context: bool = False) -> bool:
    if isinstance(value, str):
        return in_context
    if isinstance(value, list):
        return any(_has_external_context(item, in_context=in_context) for item in value)
    if isinstance(value, dict):
        return (in_context and "@import" in value) or any(
            _has_external_context(item, in_context=key == "@context") for key, item in value.items()
        )
    return False
