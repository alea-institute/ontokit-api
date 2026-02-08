"""Git repository operations for ontology versioning."""

from pathlib import Path
from typing import Any

from git import Repo
from rdflib import Graph
from rdflib.compare import graph_diff, to_isomorphic


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

    def commit(self, message: str, author: str | None = None) -> str:
        """Commit current changes and return the commit hash."""
        # Stage all changes
        self.repo.index.add("*")

        # Create commit
        commit = self.repo.index.commit(message, author=author)
        return commit.hexsha

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get commit history."""
        commits = []
        for commit in self.repo.iter_commits(max_count=limit):
            commits.append({
                "hash": commit.hexsha,
                "short_hash": commit.hexsha[:8],
                "message": commit.message.strip(),
                "author": str(commit.author),
                "timestamp": commit.committed_datetime.isoformat(),
            })
        return commits

    def get_file_at_version(self, filepath: str, version: str) -> str:
        """Get file content at a specific version."""
        commit = self.repo.commit(version)
        blob = commit.tree / filepath
        return blob.data_stream.read().decode("utf-8")

    def diff_versions(self, from_version: str, to_version: str = "HEAD") -> dict[str, Any]:
        """Get diff between two versions."""
        from_commit = self.repo.commit(from_version)
        to_commit = self.repo.commit(to_version)

        diff = from_commit.diff(to_commit)

        return {
            "from_version": from_version,
            "to_version": to_version,
            "files_changed": len(diff),
            "changes": [
                {
                    "path": d.a_path or d.b_path,
                    "change_type": d.change_type,
                }
                for d in diff
            ],
        }


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
