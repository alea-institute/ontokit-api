"""Tests for ValidationService — Plan 13-01 Task 2 (VALID-01..06).

Replaces Wave 0 stubs with real async tests for all 6 validation rules
plus IRI minting utilities.
"""

from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from ontokit.schemas.generation import ValidationError
from ontokit.services.validation_service import (
    ValidationService,
    detect_project_namespace,
    mint_iri,
)

PROJECT_ID = uuid4()
BRANCH = "main"
PROJECT_NS = "http://example.org/ontology#"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service() -> tuple[ValidationService, MagicMock]:
    """Return a ValidationService with a mocked AsyncSession."""
    db = MagicMock()
    svc = ValidationService(db)
    return svc, db


def _entity(
    *,
    label: str = "New Concept",
    parent_iris: list[str] | None = None,
    labels: list[dict] | None = None,
    iri: str = "http://example.org/ontology#newconcept",
) -> dict:
    if parent_iris is None:
        parent_iris = ["http://example.org/ontology#ParentClass"]
    if labels is None:
        labels = [{"lang": "en", "value": label}]
    return {
        "label": label,
        "parent_iris": parent_iris,
        "labels": labels,
        "iri": iri,
    }


# ---------------------------------------------------------------------------
# VALID-01: parent required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid01_parent_required_rejects_no_parent():
    """VALID-01: entity with empty parent_iris fails validation."""
    svc, _ = _make_service()
    entity = _entity(parent_iris=[])

    with patch.object(svc._index, "get_ancestor_path", new=AsyncMock(return_value=[])):
        errors = await svc.validate_entity(PROJECT_ID, BRANCH, entity, PROJECT_NS)

    codes = [e.code for e in errors]
    assert "VALID-01" in codes, f"Expected VALID-01 in {codes}"


@pytest.mark.asyncio
async def test_valid01_parent_required_accepts_with_parent():
    """VALID-01: entity with non-empty parent_iris passes validation."""
    svc, _ = _make_service()
    entity = _entity(parent_iris=["http://example.org/ontology#ParentClass"])

    with patch.object(svc._index, "get_ancestor_path", new=AsyncMock(return_value=[])):
        errors = await svc.validate_entity(PROJECT_ID, BRANCH, entity, PROJECT_NS)

    codes = [e.code for e in errors]
    assert "VALID-01" not in codes, f"Unexpected VALID-01 in {codes}"


# ---------------------------------------------------------------------------
# VALID-02: English label required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid02_english_label_rejects_missing():
    """VALID-02: entity with no English label fails validation."""
    svc, _ = _make_service()
    entity = _entity(labels=[{"lang": "fr", "value": "Chose"}])

    with patch.object(svc._index, "get_ancestor_path", new=AsyncMock(return_value=[])):
        errors = await svc.validate_entity(PROJECT_ID, BRANCH, entity, PROJECT_NS)

    codes = [e.code for e in errors]
    assert "VALID-02" in codes, f"Expected VALID-02 in {codes}"


@pytest.mark.asyncio
async def test_valid02_english_label_accepts_en():
    """VALID-02: entity with lang='en' label passes validation."""
    svc, _ = _make_service()
    entity = _entity(labels=[{"lang": "en", "value": "Thing"}])

    with patch.object(svc._index, "get_ancestor_path", new=AsyncMock(return_value=[])):
        errors = await svc.validate_entity(PROJECT_ID, BRANCH, entity, PROJECT_NS)

    codes = [e.code for e in errors]
    assert "VALID-02" not in codes, f"Unexpected VALID-02 in {codes}"


@pytest.mark.asyncio
async def test_valid02_english_label_accepts_empty_lang():
    """Language-untagged labels (lang='') are treated as English (VALID-02 passes)."""
    svc, _ = _make_service()
    entity = _entity(labels=[{"lang": "", "value": "Untagged Label"}])

    with patch.object(svc._index, "get_ancestor_path", new=AsyncMock(return_value=[])):
        errors = await svc.validate_entity(PROJECT_ID, BRANCH, entity, PROJECT_NS)

    codes = [e.code for e in errors]
    assert "VALID-02" not in codes, f"Unexpected VALID-02 in {codes}"


# ---------------------------------------------------------------------------
# VALID-03: cycle detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid03_cycle_detection_blocks_cycle():
    """VALID-03: proposed parent that would create a hierarchy cycle is blocked."""
    svc, _ = _make_service()
    entity_iri = "http://example.org/ontology#ChildClass"
    parent_iri = "http://example.org/ontology#ParentClass"
    entity = _entity(iri=entity_iri, parent_iris=[parent_iri])

    # ancestor path of proposed parent contains the entity IRI → cycle
    ancestor_path = [
        {"iri": "http://example.org/ontology#RootClass"},
        {"iri": entity_iri},
    ]

    with patch.object(
        svc._index,
        "get_ancestor_path",
        new=AsyncMock(return_value=ancestor_path),
    ):
        errors = await svc.validate_entity(PROJECT_ID, BRANCH, entity, PROJECT_NS)

    codes = [e.code for e in errors]
    assert "VALID-03" in codes, f"Expected VALID-03 in {codes}"


@pytest.mark.asyncio
async def test_valid03_cycle_detection_passes_no_cycle():
    """VALID-03: proposed parent that creates no cycle passes validation."""
    svc, _ = _make_service()
    entity_iri = "http://example.org/ontology#NewClass"
    parent_iri = "http://example.org/ontology#ParentClass"
    entity = _entity(iri=entity_iri, parent_iris=[parent_iri])

    # ancestor path of proposed parent does NOT contain entity_iri
    ancestor_path = [
        {"iri": "http://example.org/ontology#RootClass"},
    ]

    with patch.object(
        svc._index,
        "get_ancestor_path",
        new=AsyncMock(return_value=ancestor_path),
    ):
        errors = await svc.validate_entity(PROJECT_ID, BRANCH, entity, PROJECT_NS)

    codes = [e.code for e in errors]
    assert "VALID-03" not in codes, f"Unexpected VALID-03 in {codes}"


# ---------------------------------------------------------------------------
# VALID-04: namespace ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid04_namespace_rejects_foreign():
    """VALID-04: IRI outside the project-owned namespace is blocked."""
    svc, _ = _make_service()
    entity = _entity(iri="http://foreign.org/ontology#SomeClass")

    with patch.object(svc._index, "get_ancestor_path", new=AsyncMock(return_value=[])):
        errors = await svc.validate_entity(
            PROJECT_ID, BRANCH, entity, "http://example.org/ontology#"
        )

    codes = [e.code for e in errors]
    assert "VALID-04" in codes, f"Expected VALID-04 in {codes}"


@pytest.mark.asyncio
async def test_valid04_namespace_accepts_owned():
    """VALID-04: IRI within the project-owned namespace passes."""
    svc, _ = _make_service()
    entity = _entity(iri="http://example.org/ontology#OwnedClass")

    with patch.object(svc._index, "get_ancestor_path", new=AsyncMock(return_value=[])):
        errors = await svc.validate_entity(
            PROJECT_ID, BRANCH, entity, "http://example.org/ontology#"
        )

    codes = [e.code for e in errors]
    assert "VALID-04" not in codes, f"Unexpected VALID-04 in {codes}"


# ---------------------------------------------------------------------------
# VALID-05: structured error format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid05_returns_structured_errors():
    """VALID-05: validation errors are returned as list[ValidationError] with field, code, message."""
    svc, _ = _make_service()
    # Trigger VALID-01 (no parents) + VALID-02 (no English label)
    entity = _entity(
        parent_iris=[],
        labels=[{"lang": "fr", "value": "Chose"}],
        iri="http://example.org/ontology#NewClass",
    )

    with patch.object(svc._index, "get_ancestor_path", new=AsyncMock(return_value=[])):
        errors = await svc.validate_entity(PROJECT_ID, BRANCH, entity, PROJECT_NS)

    assert len(errors) >= 2, f"Expected at least 2 errors, got {len(errors)}"
    for error in errors:
        assert isinstance(error, ValidationError), f"Expected ValidationError, got {type(error)}"
        assert error.field, "field must be non-empty"
        assert error.code, "code must be non-empty"
        assert error.message, "message must be non-empty"

    codes = [e.code for e in errors]
    assert "VALID-01" in codes
    assert "VALID-02" in codes


# ---------------------------------------------------------------------------
# VALID-06: IRI minting
# ---------------------------------------------------------------------------


def test_valid06_iri_minting_uses_uuid():
    """VALID-06: minted IRI matches the pattern {namespace}{uuid4}."""
    ns = "http://example.org/ontology#"
    iri = mint_iri(ns)
    assert iri.startswith(ns), f"IRI {iri!r} does not start with {ns!r}"
    local = iri[len(ns):]
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        local,
    ), f"Local name {local!r} is not a valid UUID v4"


def test_valid06_iri_minting_detects_namespace():
    """VALID-06: namespace is auto-detected from the ontology when not provided."""
    # This test covers the mint_iri auto-append behavior
    # (Full DB-backed detect_project_namespace is tested separately)
    ns_no_sep = "http://example.org/ontology"
    iri = mint_iri(ns_no_sep)
    # Must have had '#' appended
    assert iri.startswith("http://example.org/ontology#"), (
        f"Expected # appended to namespace; got {iri!r}"
    )
