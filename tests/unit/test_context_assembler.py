"""Test stubs for OntologyContextAssembler — context assembly for LLM prompts (Plan 13-02)."""

import pytest


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_assemble_context_includes_current_class():
    """Assembled context includes the current class IRI, labels, and annotations."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_assemble_context_includes_parents():
    """Assembled context includes parent class labels and IRIs."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_assemble_context_includes_siblings():
    """Assembled context includes sibling classes of the current class."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_assemble_context_includes_existing_children():
    """Assembled context lists existing children of the current class."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_assemble_context_respects_max_siblings():
    """max_siblings parameter caps the number of siblings included in the context."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_assemble_context_handles_root_class():
    """A class with no parents returns an empty parents list without error."""
