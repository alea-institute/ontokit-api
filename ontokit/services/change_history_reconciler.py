"""Reconcile entity change events against a branch's first-parent history."""

import asyncio
import json
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict, cast
from uuid import UUID

import pygit2
from arq import ArqRedis
from rdflib import Graph
from sqlalchemy import CursorResult, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ontokit.core.constants import ONTOKIT_SYNC_COMMITTER_EMAIL
from ontokit.core.limits import MAX_TURTLE_PAYLOAD_BYTES
from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.change_event import ChangeOrigin, EntityChangeEvent
from ontokit.models.change_history_watermark import ChangeHistoryWatermark
from ontokit.models.normalization import NormalizationRun
from ontokit.models.project import Project, get_git_ontology_path
from ontokit.models.pull_request import PullRequest
from ontokit.models.user_commit_identity import UserCommitIdentity
from ontokit.services.ai_origin_service import AIOriginService
from ontokit.services.change_event_service import ChangeEventService, ChangeEventSkips
from ontokit.services.commit_identity import noreply_alias
from ontokit.services.ontology import rdf_format_for_filename

logger = logging.getLogger(__name__)

RECONCILE_CHANGE_HISTORY_TASK = "reconcile_change_history_task"
_BACKFILL_DAYS = 90
# Commit times are author-controlled, so the first backfill is also bounded by
# count: a repository of recent-looking commits cannot make it walk forever.
_BACKFILL_MAX_COMMITS = 500
# History parses the same documents an import or save accepts, and no larger.
_MAX_HISTORY_BLOB_BYTES = MAX_TURTLE_PAYLOAD_BYTES
_ENQUEUE_TIMEOUT_SECONDS = 2


class ReconciliationResult(TypedDict):
    processed: int
    recorded: int
    skipped: int
    parse_errors: int
    more: bool


def _branch_head(git: BareGitRepositoryService | None, project_id: UUID, branch: str) -> str | None:
    try:
        git = git or BareGitRepositoryService()
        if not git.repository_exists(project_id):
            return None
        ref = git.get_repository(project_id).repo.references.get(f"refs/heads/{branch}")
        return str(ref.peel(pygit2.Commit).id) if ref is not None else None
    except Exception:
        return None


async def enqueue_change_history(
    project_id: UUID,
    branch: str,
    *,
    pool: ArqRedis | None = None,
    git: BareGitRepositoryService | None = None,
) -> None:
    """Queue one job per branch head, without affecting the landing operation.

    ARQ ignores a job whose id is already queued or running. An id that named
    only the branch dropped a landing made while that branch's job was
    finishing, so the id also names the head being reconciled. Jobs for one
    branch still run one at a time under the branch lock.
    """
    from ontokit.api.utils.redis import get_arq_pool

    if branch.startswith("suggest/"):
        return

    try:
        async with asyncio.timeout(_ENQUEUE_TIMEOUT_SECONDS):
            head = await asyncio.to_thread(_branch_head, git, project_id, branch)
            if pool is None:
                pool = await get_arq_pool()
            await pool.enqueue_job(
                RECONCILE_CHANGE_HISTORY_TASK,
                str(project_id),
                branch,
                _job_id=":".join(filter(None, ("change-history", str(project_id), branch, head))),
            )
    except Exception as exc:
        logger.warning(
            "Failed to queue change history for project %s branch %s (%s)",
            project_id,
            branch,
            type(exc).__name__,
        )


async def advance_inline_watermark(
    db: AsyncSession, project_id: UUID, branch: str, previous_head: str, commit_hash: str
) -> bool:
    """Advance a contiguous inline save under the source writer's branch lock.

    Returns False when the branch has no watermark yet or its watermark trails
    the save's parent. Reconciliation then establishes the baseline or catches
    up; it skips the commits an inline save already recorded.
    """
    watermark = ChangeHistoryWatermark
    result = await db.execute(
        update(watermark)
        .where(
            watermark.project_id == project_id,
            watermark.branch == branch,
            watermark.commit_hash == previous_head,
        )
        .values(commit_hash=commit_hash, updated_at=func.now())
    )
    return cast(CursorResult[Any], result).rowcount == 1


def history_branches(repo: pygit2.Repository) -> list[str]:
    """Name the local branches whose changes the feed records."""
    prefix = "refs/heads/"
    return sorted(
        name.removeprefix(prefix)
        for name in repo.references
        if name.startswith(prefix) and not name.startswith(f"{prefix}suggest/")
    )


class ChangeHistoryReconciler:
    def __init__(self, db: AsyncSession, git: BareGitRepositoryService) -> None:
        self._db = db
        self._git = git

    async def reconcile_branch(
        self, project_id: UUID, branch: str, *, max_commits: int = 50
    ) -> ReconciliationResult:
        if max_commits < 1:
            raise ValueError("max_commits must be positive")
        result = ReconciliationResult(
            processed=0, recorded=0, skipped=0, parse_errors=0, more=False
        )
        if branch.startswith("suggest/"):
            return result

        async with self._db.begin():
            # Share the source writer's key so an inline save cannot race deduplication.
            locked = await self._db.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"suggestion:{project_id}:{branch}"},
            )
            if not locked:
                # An inline writer may be about to release a new head.
                result["more"] = True
                return result
            if not self._git.repository_exists(project_id):
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

            fork_from = None
            default_branch = repository.get_default_branch()
            if watermark is None and branch != default_branch:
                # A work branch's own history starts where it left the default branch.
                default_ref = repository.repo.references.get(f"refs/heads/{default_branch}")
                if default_ref is not None:
                    fork_from = default_ref.peel(pygit2.Commit).id

            # History walks and RDF parses are CPU-bound; keep them off the
            # worker's shared event loop so other jobs keep running.
            baseline, pending = await asyncio.to_thread(
                _select_history,
                repository.repo,
                head,
                watermark.commit_hash if watermark else None,
                max_commits,
                fork_from,
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
            old_blob_id = None
            # A previous batch may have ended on a parse error. Recover the last
            # good graph while leaving those commits marked as processed.
            while baseline is not None:
                old_blob_id = _history_blob_id(baseline, filename, original_filename)
                old_graph = await self._parse(baseline, filename, original_filename)
                if old_graph is not None:
                    break
                baseline = baseline.parents[0] if baseline.parent_ids else None

            recorder = ChangeEventService(self._db)
            skips = ChangeEventSkips()
            resolve_origin = AIOriginService(self._db).resolve
            for _ in range(min(max_commits, len(pending))):
                commit = pending[-1]
                commit_hash = str(commit.id)
                existing = await self._db.scalar(
                    select(EntityChangeEvent.id)
                    .where(
                        EntityChangeEvent.project_id == project_id,
                        EntityChangeEvent.branch == branch,
                        EntityChangeEvent.commit_hash == commit_hash,
                    )
                    .limit(1)
                )
                # A pull-request merge holds this branch lock from its ref move
                # through its commit, so a merged PR's row is visible here.
                attribution = await self._attribution(project, commit) if existing is None else None

                pending.pop()
                blob_id = _history_blob_id(commit, filename, original_filename)
                if old_graph is not None and blob_id is not None and blob_id == old_blob_id:
                    # The ontology file is byte-identical, so no entity changed.
                    advance(commit_hash)
                    result["processed"] += 1
                    continue
                new_graph = await self._parse(commit, filename, original_filename)
                if new_graph is None:
                    logger.warning("Could not parse ontology at commit %s", commit_hash)
                    result["parse_errors"] += 1
                else:
                    if attribution is not None:
                        user_id, user_name, origin = attribution
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
                            skips=skips,
                        )
                        result["recorded"] += len(events)
                    else:
                        result["skipped"] += 1
                    old_graph = new_graph
                    old_blob_id = blob_id
                advance(commit_hash)
                result["processed"] += 1
            # A landing while this job is running may deduplicate against its ID.
            current_ref = repository.repo.references.get(f"refs/heads/{branch}")
            result["more"] = bool(pending) or (
                current_ref is not None and current_ref.target != head.id
            )
        skips.warn()
        return result

    async def _parse(
        self, commit: pygit2.Commit, filename: str, original_filename: str | None
    ) -> Graph | None:
        size = _history_blob_size(commit, filename, original_filename)
        if size is not None and size > _MAX_HISTORY_BLOB_BYTES:
            logger.warning(
                "Ontology at commit %s is %d bytes, over the %d-byte history limit; "
                "its changes are not recorded",
                commit.id,
                size,
                _MAX_HISTORY_BLOB_BYTES,
            )
            return None
        return await asyncio.to_thread(read_history_graph, commit, filename, original_filename)

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
            return pr.author_id, event_display_name(pr.author_name), ChangeOrigin.HUMAN

        # The "system" author stands for no account, so it never carries a
        # person's name: the feed's author list would offer that person.
        system = ("system", None, ChangeOrigin.SYSTEM)
        author = commit.author
        if author.email == ONTOKIT_SYNC_COMMITTER_EMAIL:
            return system

        user_ids = {project.owner_id, *(member.user_id for member in project.members)}
        mapped_id = next(
            (
                user_id
                for user_id in user_ids
                if user_id and noreply_alias(user_id, author.name) == author.email
            ),
            None,
        )
        if mapped_id is None:
            # Only a current owner or member can be credited. Matching any
            # account would let an imported commit claim, and reveal, it.
            verified_ids = (
                await self._db.scalars(
                    select(UserCommitIdentity.user_id)
                    .where(
                        UserCommitIdentity.user_id.in_([u for u in user_ids if u]),
                        UserCommitIdentity.commit_email == author.email,
                        UserCommitIdentity.commit_email_verified.is_(True),
                        UserCommitIdentity.use_verified_email.is_(True),
                    )
                    .limit(2)
                )
            ).all()
            if len(verified_ids) == 1:
                mapped_id = verified_ids[0]

        if mapped_id is not None:
            normalization = await self._db.scalar(
                select(NormalizationRun.id)
                .where(
                    NormalizationRun.project_id == project.id,
                    NormalizationRun.commit_hash == str(commit.id),
                    NormalizationRun.is_dry_run.is_(False),
                )
                .limit(1)
            )
            # An import or normalization is mechanical work a member ran:
            # they are its author, and its origin is the system.
            origin = ChangeOrigin.HUMAN if normalization is None else ChangeOrigin.SYSTEM
            return mapped_id, event_display_name(author.name), origin
        return system


def _select_history(
    repo: pygit2.Repository,
    head: pygit2.Commit,
    previous_hash: str | None,
    max_commits: int,
    fork_from: pygit2.Oid | None = None,
) -> tuple[pygit2.Commit | None, deque[pygit2.Commit]]:
    """Find the first-parent commits to record and the commit to diff the oldest against.

    With a watermark, history resumes after it. Without one, it starts at the
    backfill cutoff or, for a work branch, at the first commit it shares with
    the default branch (``fork_from``), whichever comes first.
    """
    # Keep the oldest pending batch and one lookahead while finding its baseline.
    pending: deque[pygit2.Commit] = deque(maxlen=max_commits + 1)
    cutoff = (datetime.now(UTC) - timedelta(days=_BACKFILL_DAYS)).timestamp()
    walk = repo.walk(head.id, pygit2.enums.SortMode.TOPOLOGICAL)
    walk.simplify_first_parent()
    for walked, commit in enumerate(walk):
        if previous_hash:
            is_baseline = str(commit.id) == previous_hash
        else:
            is_baseline = commit.commit_time < cutoff or (
                fork_from is not None
                and (commit.id == fork_from or repo.descendant_of(fork_from, commit.id))
            )
            if not is_baseline and walked >= _BACKFILL_MAX_COMMITS:
                logger.warning(
                    "Change history backfill of %s stops at %d commits; "
                    "history before %s is not recorded",
                    head.id,
                    _BACKFILL_MAX_COMMITS,
                    commit.id,
                )
                is_baseline = True
        if is_baseline:
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


def event_display_name(name: str | None) -> str | None:
    """Keep emails out of the event display name, including provider fallbacks."""
    return name[:255] if name and "@" not in name else None


def _history_blob(
    commit: pygit2.Commit, filename: str, original_filename: str | None
) -> pygit2.Blob | None:
    for name in (filename, original_filename):
        if not name:
            continue
        try:
            entry = commit.tree[name]
        except KeyError:
            continue
        return entry if isinstance(entry, pygit2.Blob) else None
    return None


def _history_blob_id(
    commit: pygit2.Commit, filename: str, original_filename: str | None
) -> pygit2.Oid | None:
    blob = _history_blob(commit, filename, original_filename)
    return blob.id if blob is not None else None


def _history_blob_size(
    commit: pygit2.Commit, filename: str, original_filename: str | None
) -> int | None:
    blob = _history_blob(commit, filename, original_filename)
    return blob.size if blob is not None else None


def read_history_graph(
    commit: object, filename: str, original_filename: str | None = None
) -> Graph | None:
    """Parse the ontology from an immutable commit tree."""
    if not isinstance(commit, pygit2.Commit):
        return None
    try:
        try:
            blob = commit.tree[filename]
        except KeyError:
            if not original_filename or original_filename == filename:
                raise
            filename = original_filename
            blob = commit.tree[filename]
        if not isinstance(blob, pygit2.Blob):
            return None
        content = blob.data
        rdf_format = rdf_format_for_filename(filename)
        if rdf_format == "json-ld" and _has_external_context(json.loads(content)):
            # A history parse must not fetch URLs or local files.
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
