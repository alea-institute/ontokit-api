"""GitHub sync service for periodic pull/push of GitHub-connected projects."""

import logging
import secrets
from datetime import UTC, datetime
from typing import cast

import pygit2
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services.demo_target_authorizer import (
    DemoTargetDenied,
    authorize_integration_target,
)

logger = logging.getLogger(__name__)


async def sync_github_project(
    integration: GitHubIntegration,
    pat: str,
    git_service: BareGitRepositoryService,
    db: AsyncSession,
    outbound_only: bool | None = None,
) -> dict[str, str | int | bool]:
    """Sync a single project with its GitHub remote.

    In OUTBOUND-ONLY mode (the default, R16/KD6) the local bare repository is
    canonical and the mirror is downstream: the system identity pushes history
    out, and GitHub-side changes never enter the canonical repository. Fetching
    is retained purely to detect divergence; the fast-forward and merge branches
    are unreachable. A remote that has moved ahead is reported as
    ``sync_status="diverged"`` for an operator to resolve, with the canonical
    repository untouched.

    This closes the review-bypass hole: without it, a change merged directly on
    GitHub would sync back into the canonical ontology, around the suggestion
    pipeline and its trust ladder entirely.

    Setting ``outbound_only=False`` (or ``GITHUB_MIRROR_OUTBOUND_ONLY=false``)
    restores the legacy bidirectional behavior.

    Args:
        integration: GitHubIntegration model instance
        pat: Token authenticating the push (system mirror identity, or the
            deprecated per-user PAT fallback)
        git_service: Git service for repo operations
        db: Database session for updating integration status
        outbound_only: Override the configured direction for this call

    Returns:
        Dict with sync result details
    """
    project_id = integration.project_id
    branch = integration.default_branch or "main"
    if outbound_only is None:
        outbound_only = settings.github_mirror_outbound_only

    try:
        authorization = await authorize_integration_target(
            db, integration, operation="GitHub synchronization"
        )
    except DemoTargetDenied as exc:
        integration.sync_status = "error"
        integration.sync_error = str(exc)
        await db.commit()
        return {"status": "error", "reason": "target_refused"}
    if authorization.token and not secrets.compare_digest(pat, authorization.token):
        integration.sync_status = "error"
        integration.sync_error = "GitHub synchronization refused: wrong credential class"
        await db.commit()
        return {"status": "error", "reason": "credential_refused"}

    # Check if repository exists
    if not git_service.repository_exists(project_id):
        integration.sync_status = "error"
        integration.sync_error = "Local git repository not found"
        await db.commit()
        return {"status": "error", "reason": "no_repo"}

    try:
        integration.sync_status = "syncing"
        await db.commit()

        repo = git_service.get_repository(project_id)

        # Fetch from remote
        if not repo.fetch(token=pat):
            integration.sync_status = "error"
            integration.sync_error = "Failed to fetch from remote"
            await db.commit()
            return {"status": "error", "reason": "fetch_failed"}

        # Compare local and remote refs
        local_ref_name = f"refs/heads/{branch}"
        remote_ref_name = f"refs/remotes/origin/{branch}"

        pygit2_repo = repo.repo

        try:
            local_oid = pygit2_repo.references[local_ref_name].target
        except KeyError:
            # Local branch doesn't exist — nothing to sync
            integration.sync_status = "idle"
            integration.sync_error = None
            integration.last_sync_at = datetime.now(UTC)
            await db.commit()
            return {"status": "idle", "reason": "no_local_branch"}

        try:
            remote_oid = pygit2_repo.references[remote_ref_name].target
        except KeyError:
            # Remote branch doesn't exist yet — push local
            if repo.push(
                branch=branch,
                token=pat,
                target_authorization=authorization.capability,
            ):
                integration.sync_status = "idle"
                integration.sync_error = None
                integration.last_sync_at = datetime.now(UTC)
                await db.commit()
                return {"status": "pushed", "reason": "new_remote_branch"}
            else:
                integration.sync_status = "error"
                integration.sync_error = "Failed to push to remote"
                await db.commit()
                return {"status": "error", "reason": "push_failed"}

        if local_oid == remote_oid:
            # Already in sync
            integration.sync_status = "idle"
            integration.sync_error = None
            integration.last_sync_at = datetime.now(UTC)
            await db.commit()
            return {"status": "idle", "reason": "up_to_date"}

        # Check divergence
        ahead, behind = pygit2_repo.ahead_behind(local_oid, remote_oid)

        if outbound_only and behind > 0:
            # R16: the canonical repository is never advanced from the mirror,
            # whether the remote is simply ahead or genuinely diverged. Report
            # it and leave the local refs alone.
            integration.sync_status = "diverged"
            integration.sync_error = (
                f"Remote has {behind} commit(s) not in the canonical repository. The mirror "
                "is outbound-only; resolve on the GitHub side or reset the mirror."
            )
            await db.commit()
            logger.warning(
                "Outbound-only mirror for project %s is behind by %s commit(s) — "
                "canonical repository left untouched",
                project_id,
                behind,
            )
            return {"status": "diverged", "ahead": ahead, "behind": behind}

        if behind > 0 and ahead == 0:
            # Remote is ahead, local is not — fast-forward
            pygit2_repo.references[local_ref_name].set_target(remote_oid)
            integration.sync_status = "idle"
            integration.sync_error = None
            integration.last_sync_at = datetime.now(UTC)
            await db.commit()
            return {"status": "pulled", "behind": behind}

        elif ahead > 0 and behind == 0:
            # Local is ahead — push
            if repo.push(
                branch=branch,
                token=pat,
                target_authorization=authorization.capability,
            ):
                integration.sync_status = "idle"
                integration.sync_error = None
                integration.last_sync_at = datetime.now(UTC)
                await db.commit()
                return {"status": "pushed", "ahead": ahead}
            else:
                integration.sync_status = "error"
                integration.sync_error = "Failed to push to remote"
                await db.commit()
                return {"status": "error", "reason": "push_failed"}

        else:
            # Diverged — attempt merge
            local_commit_oid = cast(pygit2.Oid, local_oid)
            remote_commit_oid = cast(pygit2.Oid, remote_oid)
            merge_result = _try_merge(pygit2_repo, local_commit_oid, remote_commit_oid, branch)
            if merge_result["conflict"]:
                integration.sync_status = "conflict"
                integration.sync_error = cast(
                    "str | None", merge_result.get("error", "Merge conflict detected")
                )
                await db.commit()
                return {"status": "conflict", "ahead": ahead, "behind": behind}

            # Merge succeeded — push the merge commit
            if repo.push(
                branch=branch,
                token=pat,
                target_authorization=authorization.capability,
            ):
                integration.sync_status = "idle"
                integration.sync_error = None
                integration.last_sync_at = datetime.now(UTC)
                await db.commit()
                return {"status": "merged_and_pushed", "ahead": ahead, "behind": behind}
            else:
                integration.sync_status = "error"
                integration.sync_error = "Merge succeeded but push failed"
                await db.commit()
                return {"status": "error", "reason": "post_merge_push_failed"}

    except Exception as e:
        logger.exception(f"Sync failed for project {project_id}: {e}")
        integration.sync_status = "error"
        integration.sync_error = str(e)[:500]
        await db.commit()
        return {"status": "error", "reason": str(e)}


def _try_merge(
    repo: pygit2.Repository,
    local_oid: pygit2.Oid,
    remote_oid: pygit2.Oid,
    branch: str,
) -> dict[str, bool | str]:
    """Attempt a merge of diverged branches.

    Returns dict with 'conflict' boolean and optional 'error' message.
    """
    try:
        merge_index = repo.merge_commits(local_oid, remote_oid)
        if merge_index.conflicts:
            conflict_paths = []
            for conflict in merge_index.conflicts:
                # conflict is (ancestor, ours, theirs) — any may be None
                for entry in conflict:
                    if entry is not None:
                        conflict_paths.append(entry.path)
                        break
            return {
                "conflict": True,
                "error": f"Conflicting files: {', '.join(set(conflict_paths))}",
            }

        # Write merged tree
        merged_tree_oid = merge_index.write_tree(repo)

        # Create merge commit
        local_commit = cast(pygit2.Commit, repo.get(local_oid))
        remote_commit = cast(pygit2.Commit, repo.get(remote_oid))
        from ontokit.core.constants import (
            ONTOKIT_SYNC_COMMITTER_EMAIL,
            ONTOKIT_SYNC_COMMITTER_NAME,
        )

        sig = pygit2.Signature(ONTOKIT_SYNC_COMMITTER_NAME, ONTOKIT_SYNC_COMMITTER_EMAIL)
        repo.create_commit(
            f"refs/heads/{branch}",
            sig,
            sig,
            f"Merge remote-tracking branch 'origin/{branch}' into {branch}",
            merged_tree_oid,
            [local_commit.id, remote_commit.id],
        )
        return {"conflict": False}

    except Exception as e:
        return {"conflict": True, "error": f"Merge failed: {e}"}
