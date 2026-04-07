"""Test stubs for SuggestionGenerationService — GEN-01 through GEN-09 and pipeline concerns (Plan 13-02)."""

import pytest


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_gen01_generate_children():
    """GEN-01: suggestion_type='children' returns child class suggestions."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_gen02_generate_siblings():
    """GEN-02: suggestion_type='siblings' returns sibling class suggestions."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_gen03_generate_annotations():
    """GEN-03: suggestion_type='annotations' returns annotation suggestions."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_gen04_generate_parents():
    """GEN-04: suggestion_type='parents' returns parent class suggestions."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_gen05_generate_edges():
    """GEN-05: suggestion_type='edges' returns edge (relationship) suggestions."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_gen06_context_included_in_prompt():
    """GEN-06: LLM prompt messages include current class, parents, and siblings context."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_gen07_prompt_templates_based_on_generative_folio():
    """GEN-07: prompt contains structured instructions (legal ontology domain, JSON output format)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_gen08_confidence_score_normalized():
    """GEN-08: confidence is a float in [0.0, 1.0] or None."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_gen09_provenance_tagged():
    """GEN-09: each suggestion carries provenance='llm-proposed'."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_batch_size_configurable():
    """D-05: batch_size parameter in range 1-10 controls the number of suggestions returned."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_auto_validate_in_pipeline():
    """D-09: pipeline runs validation + deduplication check per suggestion before returning results."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_json_parse_handles_markdown_fences():
    """Pitfall 3: markdown code fences (```json ... ```) are stripped from LLM output before JSON parsing."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 13-02")
@pytest.mark.asyncio
async def test_confidence_normalization_scales():
    """Pitfall 4: confidence values >1.0 are divided by 100 to normalize to [0.0, 1.0]."""
