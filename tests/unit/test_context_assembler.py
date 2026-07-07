"""Unit tests for OntologyContextAssembler.

Tests context assembly from OntologyIndexService (mocked).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from ontokit.services.context_assembler import OntologyContextAssembler

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

    async def get_class_detail(_project_id, _branch, iri, **_kwargs):
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

    async def get_class_children(_project_id, _branch, parent_iri, **_kwargs):
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
