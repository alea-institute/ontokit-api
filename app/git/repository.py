"""Git repository operations for ontology versioning."""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from git import Actor, Repo
from git.exc import InvalidGitRepositoryError
from rdflib import Graph
from rdflib.compare import graph_diff, to_isomorphic

from app.core.config import settings


@dataclass
class CommitInfo:
    """Information about a commit."""

    hash: str
    short_hash: str
    message: str
    author_name: str
    author_email: str
    timestamp: str


@dataclass
class DiffInfo:
    """Information about a diff between versions."""

    from_version: str
    to_version: str
    files_changed: int
    changes: list[dict[str, str]]


class OntologyRepository:
    """Manages Git operations for an ontology repository."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self._repo: Repo | None = None

    @property
    def repo(self) -> Repo:
        """Get or initialize the Git repository."""
        if self._repo is None:
            if (self.repo_path / ".git").exists():
                self._repo = Repo(self.repo_path)
            else:
                self._repo = Repo.init(self.repo_path)
        return self._repo

    @property
    def is_initialized(self) -> bool:
        """Check if the repository has been initialized."""
        return (self.repo_path / ".git").exists()

    def commit(
        self,
        message: str,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> CommitInfo:
        """
        Commit current changes and return the commit info.

        Args:
            message: Commit message
            author_name: Author's display name
            author_email: Author's email address

        Returns:
            CommitInfo with details about the created commit
        """
        # Stage all changes
        self.repo.index.add("*")

        # Create author actor if provided
        author = None
        if author_name or author_email:
            author = Actor(
                name=author_name or "Unknown",
                email=author_email or "unknown@axigraph.local",
            )

        # Create commit
        commit = self.repo.index.commit(message, author=author, committer=author)

        return CommitInfo(
            hash=commit.hexsha,
            short_hash=commit.hexsha[:8],
            message=commit.message.strip(),
            author_name=str(commit.author.name) if commit.author else "Unknown",
            author_email=str(commit.author.email) if commit.author else "",
            timestamp=commit.committed_datetime.isoformat(),
        )

    def get_history(self, limit: int = 50) -> list[CommitInfo]:
        """Get commit history."""
        commits = []
        try:
            for commit in self.repo.iter_commits(max_count=limit):
                commits.append(
                    CommitInfo(
                        hash=commit.hexsha,
                        short_hash=commit.hexsha[:8],
                        message=commit.message.strip(),
                        author_name=str(commit.author.name) if commit.author else "Unknown",
                        author_email=str(commit.author.email) if commit.author else "",
                        timestamp=commit.committed_datetime.isoformat(),
                    )
                )
        except ValueError:
            # No commits yet
            pass
        return commits

    def get_file_at_version(self, filepath: str, version: str) -> str:
        """Get file content at a specific version."""
        commit = self.repo.commit(version)
        blob = commit.tree / filepath
        return blob.data_stream.read().decode("utf-8")

    def diff_versions(self, from_version: str, to_version: str = "HEAD") -> DiffInfo:
        """Get diff between two versions."""
        from_commit = self.repo.commit(from_version)
        to_commit = self.repo.commit(to_version)

        diff = from_commit.diff(to_commit)

        return DiffInfo(
            from_version=from_version,
            to_version=to_version,
            files_changed=len(diff),
            changes=[
                {
                    "path": d.a_path or d.b_path or "",
                    "change_type": d.change_type,
                }
                for d in diff
            ],
        )

    def list_files(self, version: str = "HEAD") -> list[str]:
        """List all files at a specific version."""
        try:
            commit = self.repo.commit(version)
            return [item.path for item in commit.tree.traverse() if item.type == "blob"]
        except (ValueError, InvalidGitRepositoryError):
            return []


class GitRepositoryService:
    """
    Service for managing git repositories for projects.

    Each project gets its own git repository for tracking ontology changes.
    """

    def __init__(self, base_path: str | None = None) -> None:
        """
        Initialize the service.

        Args:
            base_path: Base path for storing repositories. Defaults to settings.
        """
        self.base_path = Path(base_path or settings.git_repos_base_path)

    def _get_project_repo_path(self, project_id: UUID) -> Path:
        """Get the repository path for a project."""
        return self.base_path / str(project_id)

    def get_repository(self, project_id: UUID) -> OntologyRepository:
        """
        Get the OntologyRepository for a project.

        Args:
            project_id: The project's UUID

        Returns:
            OntologyRepository instance for the project
        """
        repo_path = self._get_project_repo_path(project_id)
        return OntologyRepository(repo_path)

    def initialize_repository(
        self,
        project_id: UUID,
        ontology_content: bytes,
        filename: str,
        author_name: str | None = None,
        author_email: str | None = None,
        project_name: str | None = None,
    ) -> CommitInfo:
        """
        Initialize a git repository for a project with the initial ontology file.

        Args:
            project_id: The project's UUID
            ontology_content: The ontology file content
            filename: The filename to use (e.g., "ontology.ttl")
            author_name: Author's display name
            author_email: Author's email address
            project_name: Project name for the commit message

        Returns:
            CommitInfo for the initial commit
        """
        repo_path = self._get_project_repo_path(project_id)

        # Create directory if it doesn't exist
        repo_path.mkdir(parents=True, exist_ok=True)

        # Write the ontology file
        ontology_file = repo_path / filename
        ontology_file.write_bytes(ontology_content)

        # Initialize repo and create initial commit
        repo = OntologyRepository(repo_path)
        message = f"Initial import of {project_name or 'ontology'}"

        return repo.commit(
            message=message,
            author_name=author_name,
            author_email=author_email,
        )

    def commit_changes(
        self,
        project_id: UUID,
        ontology_content: bytes,
        filename: str,
        message: str,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> CommitInfo:
        """
        Commit changes to the ontology file.

        Args:
            project_id: The project's UUID
            ontology_content: The updated ontology file content
            filename: The filename to update
            message: Commit message describing the changes
            author_name: Author's display name
            author_email: Author's email address

        Returns:
            CommitInfo for the new commit
        """
        repo_path = self._get_project_repo_path(project_id)

        # Write the updated ontology file
        ontology_file = repo_path / filename
        ontology_file.write_bytes(ontology_content)

        # Commit changes
        repo = OntologyRepository(repo_path)
        return repo.commit(
            message=message,
            author_name=author_name,
            author_email=author_email,
        )

    def get_history(self, project_id: UUID, limit: int = 50) -> list[CommitInfo]:
        """
        Get commit history for a project.

        Args:
            project_id: The project's UUID
            limit: Maximum number of commits to return

        Returns:
            List of CommitInfo objects
        """
        repo = self.get_repository(project_id)
        return repo.get_history(limit=limit)

    def get_file_at_version(
        self, project_id: UUID, filename: str, version: str
    ) -> str:
        """
        Get ontology file content at a specific version.

        Args:
            project_id: The project's UUID
            filename: The filename to retrieve
            version: Git commit hash or reference

        Returns:
            File content as string
        """
        repo = self.get_repository(project_id)
        return repo.get_file_at_version(filename, version)

    def diff_versions(
        self, project_id: UUID, from_version: str, to_version: str = "HEAD"
    ) -> DiffInfo:
        """
        Get diff between two versions.

        Args:
            project_id: The project's UUID
            from_version: Starting version (commit hash)
            to_version: Ending version (commit hash or "HEAD")

        Returns:
            DiffInfo with change details
        """
        repo = self.get_repository(project_id)
        return repo.diff_versions(from_version, to_version)

    def delete_repository(self, project_id: UUID) -> None:
        """
        Delete the git repository for a project.

        Args:
            project_id: The project's UUID
        """
        repo_path = self._get_project_repo_path(project_id)
        if repo_path.exists():
            shutil.rmtree(repo_path)

    def repository_exists(self, project_id: UUID) -> bool:
        """
        Check if a repository exists for a project.

        Args:
            project_id: The project's UUID

        Returns:
            True if repository exists
        """
        repo = self.get_repository(project_id)
        return repo.is_initialized


def get_git_service() -> GitRepositoryService:
    """Factory function for dependency injection."""
    return GitRepositoryService()


def serialize_deterministic(graph: Graph) -> str:
    """
    Serialize graph to Turtle with deterministic triple ordering.

    This ensures consistent diffs in version control.
    """
    iso_graph = to_isomorphic(graph)
    return iso_graph.serialize(format="turtle")


def semantic_diff(old_graph: Graph, new_graph: Graph) -> dict[str, Any]:
    """
    Compute semantic diff between two graphs.

    Returns added/removed triples regardless of serialization order.
    """
    in_both, in_old, in_new = graph_diff(old_graph, new_graph)

    return {
        "added": [
            {"subject": str(s), "predicate": str(p), "object": str(o)}
            for s, p, o in in_new
        ],
        "removed": [
            {"subject": str(s), "predicate": str(p), "object": str(o)}
            for s, p, o in in_old
        ],
        "added_count": len(in_new),
        "removed_count": len(in_old),
        "unchanged_count": len(in_both),
    }
