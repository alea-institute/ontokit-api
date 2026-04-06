"""Tests for StructuralSimilarityService and GlossExtractionService — Plan 02 (TOOL-01, TOOL-02)."""
from unittest.mock import MagicMock, patch

import pytest

from ontokit.services.gloss_extraction_service import GlossExtractionService
from ontokit.services.structural_similarity_service import (
    StructuralSimilarityService,
    _folio_cache,
    clear_folio_cache,
)


def _make_owl_class(iri: str) -> MagicMock:
    """Create a mock OWLClass with .iri and .label attributes."""
    cls = MagicMock()
    cls.iri = iri
    cls.label = iri.split("/")[-1].split("#")[-1]
    return cls


@pytest.fixture(autouse=True)
def clear_cache():
    """Ensure FOLIO module cache is clean before and after each test."""
    clear_folio_cache()
    yield
    clear_folio_cache()


def test_structural_similarity_returns_jaccard_score():
    """Jaccard similarity computed from shared parent-class sets for two IRIs (TOOL-01).

    parents_a = {X, Y, Z}, parents_b = {Y, Z, W}
    intersection = {Y, Z}, union = {X, Y, Z, W}
    Jaccard = 2/4 = 0.5
    """
    mock_folio = MagicMock()
    mock_folio.get_parents.side_effect = lambda iri, max_depth: {
        "iri:a": [_make_owl_class("iri:X"), _make_owl_class("iri:Y"), _make_owl_class("iri:Z")],
        "iri:b": [_make_owl_class("iri:Y"), _make_owl_class("iri:Z"), _make_owl_class("iri:W")],
    }[iri]

    with patch(
        "ontokit.services.structural_similarity_service._get_folio_instance",
        return_value=mock_folio,
    ):
        service = StructuralSimilarityService()
        score = service.compute_similarity("iri:a", "iri:b")

    assert score == pytest.approx(0.5), f"Expected 0.5 but got {score}"


def test_structural_similarity_unknown_iri_returns_zero():
    """Unknown IRI with no parents in index degrades gracefully — returns 0.0 (TOOL-01)."""
    mock_folio = MagicMock()
    mock_folio.get_parents.side_effect = Exception("IRI not found in FOLIO index")

    with patch(
        "ontokit.services.structural_similarity_service._get_folio_instance",
        return_value=mock_folio,
    ):
        service = StructuralSimilarityService()
        score = service.compute_similarity("iri:unknown", "iri:other")

    assert score == 0.0


def test_structural_similarity_same_entity_returns_one():
    """Self-similarity of any IRI must equal 1.0 (TOOL-01)."""
    parents = [_make_owl_class("iri:X"), _make_owl_class("iri:Y")]
    mock_folio = MagicMock()
    mock_folio.get_parents.return_value = parents

    with patch(
        "ontokit.services.structural_similarity_service._get_folio_instance",
        return_value=mock_folio,
    ):
        service = StructuralSimilarityService()
        score = service.compute_similarity("iri:a", "iri:a")

    assert score == pytest.approx(1.0), f"Expected 1.0 but got {score}"


def test_gloss_extraction_stub_raises_not_implemented():
    """OpenGloss extractor raises NotImplementedError before Plan 02 wires the corpus (TOOL-02)."""
    service = GlossExtractionService()

    with pytest.raises(NotImplementedError) as exc_info:
        service.extract_glosses("Some reference text about legal matters.", "Contract")

    assert "OpenGloss" in str(exc_info.value)
