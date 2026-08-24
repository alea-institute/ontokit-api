"""Remote reconciliation for the pull-request GitHub mirror.

Database receipt ownership stays in :mod:`pull_request_service`; this focused
collaborator owns only the deterministic remote operation. Keeping the network
state machine separate makes every caller (create, update, close, merge, and
manual retry) replay the same durable local intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ontokit.services.github_service import GitHubPR, GitHubService

GitHubPRIntentState = Literal["open", "closed", "merged"]


@dataclass(frozen=True)
class GitHubPRSyncIntent:
    """Immutable local state used by one remote synchronization attempt."""

    title: str
    body: str | None
    head: str
    base: str
    state: GitHubPRIntentState
    merge_title: str | None
    stored_number: int | None
    stored_repo_owner: str | None
    stored_repo_name: str | None


class GitHubPullRequestReconciler:
    """Make a GitHub PR exactly reflect one durable local intent."""

    def __init__(self, github: GitHubService) -> None:
        self.github = github

    async def reconcile(
        self,
        *,
        token: str,
        owner: str,
        repo: str,
        intent: GitHubPRSyncIntent,
    ) -> GitHubPR:
        mirror = await self._resolve_verified_mirror(
            token=token,
            owner=owner,
            repo=repo,
            intent=intent,
        )
        if mirror is None:
            mirror = await self.github.create_pull_request(
                token=token,
                owner=owner,
                repo=repo,
                title=intent.title,
                head=intent.head,
                base=intent.base,
                body=intent.body,
            )

        if intent.state == "merged":
            if mirror.merged:
                return await self.github.update_pull_request(
                    token=token,
                    owner=owner,
                    repo=repo,
                    pr_number=mirror.number,
                    title=intent.title,
                    body=intent.body,
                )
            # A locally merged PR may have been closed remotely. Reopening and
            # updating metadata makes the subsequent merge replayable.
            await self.github.update_pull_request(
                token=token,
                owner=owner,
                repo=repo,
                pr_number=mirror.number,
                title=intent.title,
                body=intent.body,
                state="open",
            )
            merge_result = await self.github.merge_pull_request(
                token=token,
                owner=owner,
                repo=repo,
                pr_number=mirror.number,
                commit_title=intent.merge_title,
            )
            if merge_result.get("merged") is not True:
                raise RuntimeError("GitHub did not merge the verified pull request")
            merged = await self.github.get_pull_request(
                token=token,
                owner=owner,
                repo=repo,
                pr_number=mirror.number,
            )
            if not merged.merged:
                raise RuntimeError("GitHub merge was not visible after a successful response")
            return merged

        if mirror.merged:
            raise RuntimeError("Verified GitHub pull request is already merged")

        return await self.github.update_pull_request(
            token=token,
            owner=owner,
            repo=repo,
            pr_number=mirror.number,
            title=intent.title,
            body=intent.body,
            state=intent.state,
        )

    async def _resolve_verified_mirror(
        self,
        *,
        token: str,
        owner: str,
        repo: str,
        intent: GitHubPRSyncIntent,
    ) -> GitHubPR | None:
        """Resolve a mirror only after checking repository, head, and base."""
        stored_repo_matches = intent.stored_repo_owner in (
            None,
            owner,
        ) and intent.stored_repo_name in (None, repo)
        if intent.stored_number is not None and stored_repo_matches:
            stored = await self.github.get_pull_request_or_none(
                token=token,
                owner=owner,
                repo=repo,
                pr_number=intent.stored_number,
            )
            if stored is not None and self._is_exact_match(stored, intent):
                return stored

        candidates = await self.github.list_pull_requests(
            token=token,
            owner=owner,
            repo=repo,
            state="all",
            head=f"{owner}:{intent.head}",
            base=intent.base,
        )
        exact_matches = [
            candidate for candidate in candidates if self._is_exact_match(candidate, intent)
        ]
        if not exact_matches:
            return None
        return max(exact_matches, key=lambda match: (match.updated_at, match.number))

    @staticmethod
    def _is_exact_match(candidate: GitHubPR, intent: GitHubPRSyncIntent) -> bool:
        return candidate.head_ref == intent.head and candidate.base_ref == intent.base
