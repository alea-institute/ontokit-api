"""Unit tests for OntologyContextAssembler and quality_filter.

Tests context assembly from OntologyIndexService (mocked) and
quality filter heuristic scoring ported from generative-folio.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from ontokit.services.context_assembler import OntologyContextAssembler
from ontokit.services.quality_filter import (
    ACCEPT_THRESHOLD,
    FOLIO_AREAS_OF_LAW,
    LEGAL_DEFINITION_KEYWORDS,
    LEGAL_SOURCE_PATTERNS,
    REJECT_THRESHOLD,
    compute_legal_score,
    is_legal_concept,
)

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
BRANCH = "main"
CLASS_IRI = "http://example.org/ontology#TestClass"
PARENT_IRI = "http://example.org/ontology#ParentClass"
SIBLING_IRI = "http://example.org/ontology#SiblingClass"
CHILD_IRI = "http://example.org/ontology#ChildClass"


def _make_assembler(index_mock: MagicMock) -> OntologyContextAssembler:
    """Create an OntologyContextAssembler with a mocked DB session."""
    db_mock = MagicMock()
    assembler = OntologyContextAssembler(db_mock)
    # Replace the internal index service with our mock
    assembler._index = index_mock
    return assembler


def _make_index_mock(
    *,
    class_detail: dict | None,
    parent_detail: dict | None = None,
    children: list | None = None,
    siblings: list | None = None,
) -> MagicMock:
    """Build a MagicMock OntologyIndexService with configurable returns."""
    index = MagicMock()

    async def get_class_detail(project_id, branch, iri, **kwargs):
        if iri == CLASS_IRI:
            return class_detail
        if iri == PARENT_IRI:
            return parent_detail or {
                "iri": PARENT_IRI,
                "labels": [{"value": "Parent Class", "lang": "en"}],
                "annotations": [],
                "parent_iris": [],
                "parent_labels": {},
            }
        return None

    index.get_class_detail = AsyncMock(side_effect=get_class_detail)

    async def get_class_children(project_id, branch, parent_iri, **kwargs):
        if parent_iri == PARENT_IRI:
            return siblings or [
                {"iri": SIBLING_IRI, "label": "Sibling Class", "child_count": 0},
            ]
        if parent_iri == CLASS_IRI:
            return children or [
                {"iri": CHILD_IRI, "label": "Child Class", "child_count": 0},
            ]
        return []

    index.get_class_children = AsyncMock(side_effect=get_class_children)

    return index


# ---------------------------------------------------------------------------
# test_assemble_context_includes_current_class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_context_includes_current_class():
    """result["current_class"]["iri"] == class_iri, labels non-empty."""
    index_mock = _make_index_mock(
        class_detail={
            "iri": CLASS_IRI,
            "labels": [{"value": "Test Class", "lang": "en"}],
            "annotations": [],
            "parent_iris": [PARENT_IRI],
            "parent_labels": {PARENT_IRI: "Parent Class"},
        }
    )
    assembler = _make_assembler(index_mock)
    result = await assembler.assemble(PROJECT_ID, BRANCH, CLASS_IRI)

    assert result["current_class"]["iri"] == CLASS_IRI
    assert len(result["current_class"]["labels"]) > 0
    assert result["current_class"]["labels"][0]["value"] == "Test Class"


# ---------------------------------------------------------------------------
# test_assemble_context_includes_parents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_context_includes_parents():
    """result["parents"] is list of dicts with iri and label keys."""
    index_mock = _make_index_mock(
        class_detail={
            "iri": CLASS_IRI,
            "labels": [{"value": "Test Class", "lang": "en"}],
            "annotations": [],
            "parent_iris": [PARENT_IRI],
            "parent_labels": {PARENT_IRI: "Parent Class"},
        }
    )
    assembler = _make_assembler(index_mock)
    result = await assembler.assemble(PROJECT_ID, BRANCH, CLASS_IRI)

    assert isinstance(result["parents"], list)
    assert len(result["parents"]) >= 1
    assert "iri" in result["parents"][0]
    assert "label" in result["parents"][0]
    assert result["parents"][0]["iri"] == PARENT_IRI


# ---------------------------------------------------------------------------
# test_assemble_context_includes_siblings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_context_includes_siblings():
    """result["siblings"] is list, excludes current class IRI."""
    index_mock = _make_index_mock(
        class_detail={
            "iri": CLASS_IRI,
            "labels": [{"value": "Test Class", "lang": "en"}],
            "annotations": [],
            "parent_iris": [PARENT_IRI],
            "parent_labels": {PARENT_IRI: "Parent Class"},
        },
        siblings=[
            {"iri": CLASS_IRI, "label": "Test Class", "child_count": 0},  # self — should be excluded
            {"iri": SIBLING_IRI, "label": "Sibling Class", "child_count": 0},
        ],
    )
    assembler = _make_assembler(index_mock)
    result = await assembler.assemble(PROJECT_ID, BRANCH, CLASS_IRI)

    assert isinstance(result["siblings"], list)
    sibling_iris = [s["iri"] for s in result["siblings"]]
    assert CLASS_IRI not in sibling_iris
    assert SIBLING_IRI in sibling_iris


# ---------------------------------------------------------------------------
# test_assemble_context_includes_existing_children
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_context_includes_existing_children():
    """result["existing_children"] is list."""
    index_mock = _make_index_mock(
        class_detail={
            "iri": CLASS_IRI,
            "labels": [{"value": "Test Class", "lang": "en"}],
            "annotations": [],
            "parent_iris": [PARENT_IRI],
            "parent_labels": {PARENT_IRI: "Parent Class"},
        }
    )
    assembler = _make_assembler(index_mock)
    result = await assembler.assemble(PROJECT_ID, BRANCH, CLASS_IRI)

    assert isinstance(result["existing_children"], list)
    child_iris = [c["iri"] for c in result["existing_children"]]
    assert CHILD_IRI in child_iris


# ---------------------------------------------------------------------------
# test_assemble_context_respects_max_siblings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_context_respects_max_siblings():
    """len(result["siblings"]) <= max_siblings."""
    many_siblings = [
        {"iri": f"http://example.org/ontology#Sib{i}", "label": f"Sib {i}", "child_count": 0}
        for i in range(25)
    ]
    index_mock = _make_index_mock(
        class_detail={
            "iri": CLASS_IRI,
            "labels": [{"value": "Test Class", "lang": "en"}],
            "annotations": [],
            "parent_iris": [PARENT_IRI],
            "parent_labels": {PARENT_IRI: "Parent Class"},
        },
        siblings=many_siblings,
    )
    assembler = _make_assembler(index_mock)
    result = await assembler.assemble(PROJECT_ID, BRANCH, CLASS_IRI, max_siblings=5)

    assert len(result["siblings"]) <= 5


# ---------------------------------------------------------------------------
# test_assemble_context_handles_root_class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_context_handles_root_class():
    """Class with no parents returns result["parents"] == [] and siblings == []."""
    index_mock = _make_index_mock(
        class_detail={
            "iri": CLASS_IRI,
            "labels": [{"value": "Root Class", "lang": "en"}],
            "annotations": [],
            "parent_iris": [],
            "parent_labels": {},
        }
    )
    assembler = _make_assembler(index_mock)
    result = await assembler.assemble(PROJECT_ID, BRANCH, CLASS_IRI)

    assert result["parents"] == []
    assert result["siblings"] == []


# ---------------------------------------------------------------------------
# Quality filter sanity checks
# ---------------------------------------------------------------------------


def test_quality_filter_keyword_count():
    """LEGAL_DEFINITION_KEYWORDS has >= 90 entries (frozenset deduplicates duplicate keywords)."""
    assert len(LEGAL_DEFINITION_KEYWORDS) >= 90


def test_quality_filter_areas_of_law():
    """FOLIO_AREAS_OF_LAW has >= 31 entries."""
    assert len(FOLIO_AREAS_OF_LAW) >= 31


def test_quality_filter_source_patterns():
    """LEGAL_SOURCE_PATTERNS has >= 13 compiled regex patterns."""
    assert len(LEGAL_SOURCE_PATTERNS) >= 13
    for p in LEGAL_SOURCE_PATTERNS:
        assert isinstance(p, re.Pattern)


def test_compute_legal_score_high():
    """A clearly legal definition with known area scores >= ACCEPT_THRESHOLD."""
    score = compute_legal_score(
        definition="A contract is a legally binding agreement that creates obligations and rights between parties.",
        areas_of_law=["Contract Law"],
        sources=["Restatement (Second) of Contracts § 1"],
        has_jurisdictions=True,
        has_etymology=True,
        has_notes=True,
    )
    assert score >= ACCEPT_THRESHOLD


def test_compute_legal_score_low():
    """A non-legal term scores below ACCEPT_THRESHOLD."""
    score = compute_legal_score(
        definition="JPEG is a compressed image file format commonly used for photographs.",
        areas_of_law=[],
        sources=[],
        has_jurisdictions=False,
        has_etymology=False,
        has_notes=False,
    )
    assert score < ACCEPT_THRESHOLD


def test_is_legal_concept_true():
    """is_legal_concept returns True for a clearly legal concept."""
    assert is_legal_concept(
        definition="Negligence is the breach of a duty of care owed to another, resulting in tort liability.",
        areas_of_law=["Tort Law"],
        sources=["Restatement (Third) of Torts"],
        has_jurisdictions=True,
        has_etymology=False,
        has_notes=False,
    )


def test_is_legal_concept_false():
    """is_legal_concept returns False for a non-legal concept."""
    assert not is_legal_concept(
        definition="A VHS tape is a magnetic media format for recording video.",
        areas_of_law=[],
        sources=[],
        has_jurisdictions=False,
        has_etymology=False,
        has_notes=False,
    )
