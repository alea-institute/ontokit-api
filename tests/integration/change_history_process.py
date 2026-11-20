"""Run one change-history actor in its own process for the cross-process tests.

Usage: ``python -m tests.integration.change_history_process MODE REPOS PROJECT BRANCH``.
The database comes from ``DATABASE_URL``. Each mode prints one JSON line per step.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from uuid import UUID

import pygit2
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.pull_request import PullRequest
from ontokit.services.branch_lock import branch_write_lock
from ontokit.services.change_history_reconciler import ChangeHistoryReconciler

TURTLE = "@prefix ex: <https://example.org/ontology#> .\n@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"


def say(**fields: object) -> None:
    print(json.dumps(fields), flush=True)


def commit(
    git: BareGitRepositoryService,
    project_id: UUID,
    branch: str,
    classes: int,
    *,
    second_parent: pygit2.Oid | None = None,
) -> pygit2.Oid:
    """Commit an ontology that declares ``classes`` classes on top of the branch."""
    repo = git.get_repository(project_id).repo
    ref = f"refs/heads/{branch}"
    parents = [repo.references[ref].target] if ref in repo.references else []
    if second_parent is not None:
        parents.append(second_parent)
    content = TURTLE + "".join(f"ex:Class{number} a owl:Class .\n" for number in range(classes))
    tree = repo.TreeBuilder()
    tree.insert("ontology.ttl", repo.create_blob(content.encode()), pygit2.GIT_FILEMODE_BLOB)
    signature = pygit2.Signature("Remote Contributor", "remote@example.org")
    return repo.create_commit(ref, signature, signature, "Ontology update", tree.write(), parents)


async def reconcile_until_caught_up(
    sessions: async_sessionmaker, git: BareGitRepositoryService, project_id: UUID, branch: str
) -> None:
    """Behave as the worker does: run a batch and retry while more remains."""
    start_at, max_commits = float(sys.argv[5]), int(sys.argv[6])
    await asyncio.sleep(max(0.0, start_at - time.time()))
    recorded = processed = contended = 0
    more = True
    while more:
        async with sessions() as db:
            result = await ChangeHistoryReconciler(db, git).reconcile_branch(
                project_id, branch, max_commits=max_commits
            )
        recorded += result["recorded"]
        processed += result["processed"]
        more = result["more"]
        if more and not result["processed"]:
            contended += 1
            await asyncio.sleep(0.005)
    say(recorded=recorded, processed=processed, contended=contended)


async def reconcile_once(
    sessions: async_sessionmaker, git: BareGitRepositoryService, project_id: UUID, branch: str
) -> None:
    async with sessions() as db:
        say(**await ChangeHistoryReconciler(db, git).reconcile_branch(project_id, branch))


async def merge_while_observed(
    sessions: async_sessionmaker, git: BareGitRepositoryService, project_id: UUID, branch: str
) -> None:
    """Land a merge the way the pull-request service does, pausing mid-transaction."""
    side = git.get_repository(project_id).repo.references["refs/heads/proposal"].target
    async with sessions() as db, branch_write_lock(db, project_id, branch):
        merged = commit(git, project_id, branch, 2, second_parent=side)
        say(step="ref moved", merge_commit=str(merged))
        sys.stdin.readline()
        db.add(
            PullRequest(
                project_id=project_id,
                pr_number=1,
                title="Proposal",
                source_branch="proposal",
                target_branch=branch,
                author_id="contributor",
                author_name="Contributor",
                status="merged",
                merge_commit_hash=str(merged),
            )
        )
        await db.commit()
    say(step="committed")


MODES = {
    "reconcile": reconcile_until_caught_up,
    "once": reconcile_once,
    "merge": merge_while_observed,
}


async def main() -> None:
    mode, repos, project, branch = sys.argv[1:5]
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        await MODES[mode](
            async_sessionmaker(engine, expire_on_commit=False),
            BareGitRepositoryService(repos),
            UUID(project),
            branch,
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
