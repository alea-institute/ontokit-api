"""Wave 0 stubs for structural similarity and gloss extraction — Plans 01, 02 (TOOL-01, TOOL-02)."""
import pytest


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_structural_similarity_returns_jaccard_score():
    """Jaccard similarity computed from shared parent-class sets for two IRIs (TOOL-01)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_structural_similarity_unknown_iri_returns_zero():
    """Unknown IRI with no parents in index degrades gracefully — returns 0.0 (TOOL-01)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_structural_similarity_same_entity_returns_one():
    """Self-similarity of any IRI must equal 1.0 (TOOL-01)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_gloss_extraction_stub_raises_not_implemented():
    """OpenGloss extractor raises NotImplementedError before Plan 02 wires the corpus (TOOL-02)."""
