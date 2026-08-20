"""Immutable demo-repository target contract shared by outbound write paths."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

DEMO_REPOSITORY_PAIRS = {
    ("alea-institute", "folio"): ("alea-institute", "ontokit-demo-folio"),
    ("catholicos", "ontology-semantic-canon"): (
        "alea-institute",
        "ontokit-demo-semantic-canon",
    ),
}
DEMO_REPOSITORIES = frozenset(DEMO_REPOSITORY_PAIRS.values())


def normalize_repository(owner: str, repo: str) -> tuple[str, str]:
    return owner.strip().lower(), repo.removesuffix(".git").strip().lower()


def is_demo_repository(owner: str, repo: str) -> bool:
    return normalize_repository(owner, repo) in DEMO_REPOSITORIES


def repository_from_remote_url(url: str) -> tuple[str, str] | None:
    """Extract a GitHub owner/repository pair from HTTPS, SSH, or SCP syntax."""
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", url.strip())
    if match is None:
        return None
    return normalize_repository(match.group(1), match.group(2))


@dataclass(frozen=True)
class DemoTargetAuthorization:
    """Capability issued only after a demo project's DB target has been checked."""

    project_id: UUID
    owner: str
    repo: str

    def permits(self, owner: str, repo: str) -> bool:
        return normalize_repository(owner, repo) == normalize_repository(self.owner, self.repo)


def refuse_unscoped_demo_target(owner: str, repo: str, operation: str) -> None:
    """Keep generic writers, such as PR Party, away from demo repositories."""
    if is_demo_repository(owner, repo):
        raise PermissionError(
            f"{operation} refused for demo repository {owner}/{repo} without project context"
        )
