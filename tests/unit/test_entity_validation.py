"""Test stubs for ValidationService — VALID-01 through VALID-06 (Plan 13-01)."""

import pytest


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid01_parent_required_rejects_no_parent():
    """VALID-01: entity with empty parent_iris fails validation."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid01_parent_required_accepts_with_parent():
    """VALID-01: entity with non-empty parent_iris passes validation."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid02_english_label_rejects_missing():
    """VALID-02: entity with no English label fails validation."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid02_english_label_accepts_en():
    """VALID-02: entity with lang='en' label passes validation."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid03_cycle_detection_blocks_cycle():
    """VALID-03: proposed parent that would create a hierarchy cycle is blocked."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid03_cycle_detection_passes_no_cycle():
    """VALID-03: proposed parent that creates no cycle passes validation."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid04_namespace_rejects_foreign():
    """VALID-04: IRI outside the project-owned namespace is blocked."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid04_namespace_accepts_owned():
    """VALID-04: IRI within the project-owned namespace passes."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid05_returns_structured_errors():
    """VALID-05: validation errors are returned as list[ValidationError] with field, code, message."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid06_iri_minting_uses_uuid():
    """VALID-06: minted IRI matches the pattern {namespace}{uuid4}."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-01")
@pytest.mark.asyncio
async def test_valid06_iri_minting_detects_namespace():
    """VALID-06: namespace is auto-detected from the ontology when not provided."""
