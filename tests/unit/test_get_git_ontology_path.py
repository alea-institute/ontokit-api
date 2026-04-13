"""Tests for the standalone get_git_ontology_path function in models/project.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from ontokit.models.project import get_git_ontology_path


def _make_project(
    *,
    source_file_path: str | None = None,
    github_integration: MagicMock | None = None,
) -> MagicMock:
    """Create a mock Project with the given attributes."""
    project = MagicMock()
    project.source_file_path = source_file_path
    project.github_integration = github_integration
    return project


class TestGetGitOntologyPath:
    """Tests for the standalone get_git_ontology_path() function."""

    def test_github_integration_turtle_file_path(self) -> None:
        """Returns turtle_file_path when github_integration has it set."""
        integration = MagicMock()
        integration.turtle_file_path = "src/ontology.ttl"
        integration.ontology_file_path = "src/ontology.owl"
        project = _make_project(github_integration=integration)

        result = get_git_ontology_path(project)
        assert result == "src/ontology.ttl"

    def test_github_integration_ontology_file_path_fallback(self) -> None:
        """Falls back to ontology_file_path when turtle_file_path is None."""
        integration = MagicMock()
        integration.turtle_file_path = None
        integration.ontology_file_path = "src/ontology.owl"
        project = _make_project(github_integration=integration)

        result = get_git_ontology_path(project)
        assert result == "src/ontology.owl"

    def test_source_file_path_basename(self) -> None:
        """Uses basename of source_file_path when no GitHub integration."""
        project = _make_project(source_file_path="projects/abc/ontology.ttl")

        result = get_git_ontology_path(project)
        assert result == "ontology.ttl"

    def test_default_fallback(self) -> None:
        """Returns 'ontology.ttl' when no integration and no source_file_path."""
        project = _make_project()

        result = get_git_ontology_path(project)
        assert result == "ontology.ttl"

    def test_github_integration_both_paths_none(self) -> None:
        """Falls through to source_file_path when both integration paths are None."""
        integration = MagicMock()
        integration.turtle_file_path = None
        integration.ontology_file_path = None
        project = _make_project(
            source_file_path="uploads/my-project/my-onto.ttl",
            github_integration=integration,
        )

        result = get_git_ontology_path(project)
        assert result == "my-onto.ttl"

    def test_github_integration_both_paths_none_no_source(self) -> None:
        """Falls through to default when integration paths are None and no source."""
        integration = MagicMock()
        integration.turtle_file_path = None
        integration.ontology_file_path = None
        project = _make_project(github_integration=integration)

        result = get_git_ontology_path(project)
        assert result == "ontology.ttl"
