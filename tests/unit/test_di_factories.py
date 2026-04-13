"""Tests for DI factory functions that create service instances."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from ontokit.api.routes.projects import get_change_events, get_index, get_indexed_ontology
from ontokit.services.change_event_service import ChangeEventService
from ontokit.services.ontology_index import OntologyIndexService


class TestProjectsDIFactories:
    """Tests for DI factory functions in projects routes."""

    def test_get_index_returns_ontology_index_service(self) -> None:
        """get_index() returns an OntologyIndexService with the provided db session."""
        mock_db = AsyncMock()
        result = get_index(mock_db)
        assert isinstance(result, OntologyIndexService)

    def test_get_change_events_returns_change_event_service(self) -> None:
        """get_change_events() returns a ChangeEventService with the provided db session."""
        mock_db = AsyncMock()
        result = get_change_events(mock_db)
        assert isinstance(result, ChangeEventService)

    @patch("ontokit.api.routes.projects.get_ontology_service")
    @patch("ontokit.api.routes.projects.get_storage_service")
    def test_get_indexed_ontology_returns_indexed_ontology_service(
        self,
        mock_storage: MagicMock,  # noqa: ARG002
        mock_ontology: MagicMock,  # noqa: ARG002
    ) -> None:
        """get_indexed_ontology() returns an IndexedOntologyService."""
        from ontokit.services.indexed_ontology import IndexedOntologyService

        mock_db = AsyncMock()
        result = get_indexed_ontology(mock_db)
        assert isinstance(result, IndexedOntologyService)


class TestEmbeddingsDIFactory:
    """Tests for DI factory in embeddings routes."""

    def test_get_embeddings_returns_embedding_service(self) -> None:
        """get_embeddings() returns an EmbeddingService."""
        from ontokit.api.routes.embeddings import get_embeddings
        from ontokit.services.embedding_service import EmbeddingService

        mock_db = AsyncMock()
        result = get_embeddings(mock_db)
        assert isinstance(result, EmbeddingService)


class TestSemanticSearchDIFactory:
    """Tests for DI factory in semantic_search routes."""

    def test_get_embeddings_returns_embedding_service(self) -> None:
        """get_embeddings() in semantic_search returns an EmbeddingService."""
        from ontokit.api.routes.semantic_search import get_embeddings
        from ontokit.services.embedding_service import EmbeddingService

        mock_db = AsyncMock()
        result = get_embeddings(mock_db)
        assert isinstance(result, EmbeddingService)


class TestAnalyticsDIFactory:
    """Tests for DI factory in analytics routes."""

    def test_get_change_events_returns_change_event_service(self) -> None:
        """get_change_events() in analytics returns a ChangeEventService."""
        from ontokit.api.routes.analytics import get_change_events as analytics_get_change_events

        mock_db = AsyncMock()
        result = analytics_get_change_events(mock_db)
        assert isinstance(result, ChangeEventService)
