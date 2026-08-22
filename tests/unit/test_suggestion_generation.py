"""Tests for SuggestionGenerationService — GEN-01 through GEN-09 and pipeline concerns (Plan 13-03)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from ontokit.schemas.generation import (
    GenerateSuggestionsResponse,
)
from ontokit.services.suggestion_generation_service import SuggestionGenerationService

PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")
CLASS_IRI = "http://example.org/ontology#ParentClass"
NAMESPACE = "http://example.org/ontology#"


def _make_service(_mock_llm_provider, mock_assembler, mock_validator, mock_dedup):
    """Construct a SuggestionGenerationService with all dependencies mocked."""
    db = AsyncMock()
    return SuggestionGenerationService(
        db=db,
        assembler=mock_assembler,
        validator=mock_validator,
        dedup_service=mock_dedup,
    )


def _make_context(label: str = "Parent Class"):
    """Minimal context dict matching OntologyContextAssembler.assemble() output."""
    return {
        "current_class": {
            "iri": CLASS_IRI,
            "labels": [{"value": label, "lang": "en"}],
            "annotations": [],
        },
        "parents": [
            {
                "iri": "http://example.org/ontology#GrandParent",
                "label": "Grand Parent",
                "annotations": [],
            }
        ],
        "siblings": [{"iri": "http://example.org/ontology#SiblingClass", "label": "Sibling Class"}],
        "existing_children": [],
    }


def _make_llm_json(suggestions: list[dict]) -> str:
    """Return JSON string of the LLM-standard suggestions envelope."""
    return json.dumps({"suggestions": suggestions})


def _suggestion(label: str, confidence: float | None = 0.9) -> dict:
    """children/siblings/parents-shaped raw suggestion (label + definition)."""
    return {
        "label": label,
        "definition": f"Definition of {label}",
        "confidence": confidence,
    }


def _parent_suggestion(label: str, iri: str | None = None, confidence: float | None = 0.9) -> dict:
    """parents-shaped raw suggestion — carries an optional existing-class `iri`."""
    return {
        "label": label,
        "iri": iri,
        "definition": f"Definition of {label}",
        "confidence": confidence,
    }


def _annotation_suggestion(
    property_iri: str, value: str, lang: str | None = None, confidence: float | None = 0.9
) -> dict:
    """annotations-shaped raw suggestion — matches prompts/annotations.py output."""
    return {"property_iri": property_iri, "value": value, "lang": lang, "confidence": confidence}


def _edge_suggestion(
    target_label: str,
    target_iri: str | None = "http://example.org/ontology#Target",
    relationship_type: str = "seeAlso",
    confidence: float | None = 0.9,
) -> dict:
    """edges-shaped raw suggestion — matches prompts/edges.py output."""
    return {
        "target_label": target_label,
        "target_iri": target_iri,
        "relationship_type": relationship_type,
        "explanation": f"Why {target_label} relates",
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# GEN-01: children
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gen01_generate_children(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """GEN-01: suggestion_type='children' returns GenerateSuggestionsResponse with suggestions."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    mock_llm_provider.chat = AsyncMock(
        return_value=(_make_llm_json([_suggestion("Child A"), _suggestion("Child B")]), 120, 60)
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert isinstance(resp, GenerateSuggestionsResponse)
    assert len(resp.suggestions) == 2
    assert resp.input_tokens == 120
    assert resp.output_tokens == 60
    assert all(s.suggestion_type == "children" for s in resp.suggestions)


# ---------------------------------------------------------------------------
# GEN-02: siblings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gen02_generate_siblings(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """GEN-02: suggestion_type='siblings' returns sibling class suggestions."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    mock_llm_provider.chat = AsyncMock(
        return_value=(_make_llm_json([_suggestion("Sibling X")]), 100, 50)
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="siblings",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert len(resp.suggestions) >= 1
    assert all(s.suggestion_type == "siblings" for s in resp.suggestions)


# ---------------------------------------------------------------------------
# GEN-03: annotations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gen03_generate_annotations(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """GEN-03: suggestion_type='annotations' returns annotation suggestions."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    mock_llm_provider.chat = AsyncMock(
        return_value=(
            _make_llm_json(
                [
                    _annotation_suggestion("skos:altLabel", "Contrato", lang="es"),
                    _annotation_suggestion("skos:definition", "A binding agreement"),
                ]
            ),
            80,
            40,
        )
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="annotations",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert len(resp.suggestions) == 2
    assert all(s.suggestion_type == "annotations" for s in resp.suggestions)
    # The annotation payload must survive — property_iri/value/lang, not dropped.
    first = resp.suggestions[0]
    assert first.property_iri == "skos:altLabel"
    assert first.value == "Contrato"
    assert first.lang == "es"
    # An annotation is a value ON the focus class, so it carries the class IRI.
    assert first.iri == CLASS_IRI
    # Display label falls back to the value (the web renders value ?? label).
    assert first.label == "Contrato"
    # English (untagged) annotation → lang is None.
    assert resp.suggestions[1].lang is None


# ---------------------------------------------------------------------------
# GEN-04: parents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gen04_generate_parents(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """GEN-04: suggestion_type='parents' returns parent class suggestions."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    existing_parent = "http://example.org/ontology#ExistingParent"
    mock_llm_provider.chat = AsyncMock(
        return_value=(
            _make_llm_json(
                [
                    _parent_suggestion("Existing Parent", iri=existing_parent),
                    _parent_suggestion("Brand New Parent", iri=None),
                ]
            ),
            90,
            45,
        )
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="parents",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert len(resp.suggestions) == 2
    assert all(s.suggestion_type == "parents" for s in resp.suggestions)
    # An existing parent is LINKED by its IRI, not minted as a duplicate node.
    assert resp.suggestions[0].iri == existing_parent
    # A brand-new parent gets a freshly minted IRI in the project namespace.
    assert resp.suggestions[1].iri.startswith(NAMESPACE)
    assert resp.suggestions[1].iri != existing_parent


# ---------------------------------------------------------------------------
# GEN-05: edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gen05_generate_edges(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """GEN-05: suggestion_type='edges' returns edge (relationship) suggestions."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    mock_llm_provider.chat = AsyncMock(
        return_value=(
            _make_llm_json(
                [
                    _edge_suggestion(
                        "Related Concept",
                        target_iri="http://example.org/ontology#Related",
                        relationship_type="seeAlso",
                    )
                ]
            ),
            110,
            55,
        )
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="edges",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert len(resp.suggestions) == 1
    edge = resp.suggestions[0]
    assert edge.suggestion_type == "edges"
    # The edge payload must survive — target + controlled relationship type.
    assert edge.target_iri == "http://example.org/ontology#Related"
    assert edge.relationship_type == "seeAlso"
    assert edge.label == "Related Concept"  # display label = target_label
    # A well-formed edge to an existing target has no validation errors.
    assert edge.validation_errors == []


@pytest.mark.asyncio
async def test_gen05_edges_flag_uncontrolled_type_and_missing_target(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """An edge with a non-controlled relationship_type or null target_iri is
    still returned but carries GEN-05 validation errors (not silently dropped)."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    mock_llm_provider.chat = AsyncMock(
        return_value=(
            _make_llm_json(
                [_edge_suggestion("Bad Edge", target_iri=None, relationship_type="sameAs")]
            ),
            60,
            30,
        )
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="edges",
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert len(resp.suggestions) == 1
    codes = {e.field for e in resp.suggestions[0].validation_errors}
    assert "relationship_type" in codes  # sameAs is explicitly not controlled
    assert "target_iri" in codes  # null target


# ---------------------------------------------------------------------------
# GEN-06: context included in prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gen06_context_included_in_prompt(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """GEN-06: LLM prompt messages include current class, parents, and siblings context."""
    context = _make_context("My Class Label")
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=context)
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    captured_messages = []

    async def capture_chat(messages, **_kwargs):
        captured_messages.extend(messages)
        return (_make_llm_json([_suggestion("Child A")]), 100, 50)

    mock_llm_provider.chat = capture_chat

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    # Check that at least one message contains class label info
    full_text = " ".join(m["content"] for m in captured_messages)
    assert "My Class Label" in full_text or CLASS_IRI in full_text, (
        "Prompt should contain current class label or IRI"
    )


# ---------------------------------------------------------------------------
# GEN-07: prompt templates based on generative-folio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gen07_prompt_templates_based_on_generative_folio(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """GEN-07: prompt contains structured instructions (legal ontology domain, JSON output format)."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    captured_messages = []

    async def capture_chat(messages, **_kwargs):
        captured_messages.extend(messages)
        return (_make_llm_json([_suggestion("Child A")]), 100, 50)

    mock_llm_provider.chat = capture_chat

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    # The system prompt should mention "legal" domain and "JSON" output format
    full_text = " ".join(m["content"] for m in captured_messages).lower()
    assert "legal" in full_text, "Prompt should mention 'legal' domain context"
    assert "json" in full_text, "Prompt should specify JSON output format"
    assert "ontology" in full_text, "Prompt should mention 'ontology'"


# ---------------------------------------------------------------------------
# GEN-08: confidence score normalized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gen08_confidence_score_normalized(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """GEN-08: confidence is a float in [0.0, 1.0] or None."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    # LLM returns confidence as 85 (out of 100-scale) — should be normalized to 0.85
    raw = _suggestion("Child A", confidence=85)
    mock_llm_provider.chat = AsyncMock(return_value=(_make_llm_json([raw]), 100, 50))

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert len(resp.suggestions) == 1
    s = resp.suggestions[0]
    assert s.confidence is not None
    assert 0.0 <= s.confidence <= 1.0, f"Confidence {s.confidence} is out of [0, 1] range"
    # 85 / 100 = 0.85
    assert abs(s.confidence - 0.85) < 1e-6, f"Expected 0.85, got {s.confidence}"


# ---------------------------------------------------------------------------
# GEN-09: provenance tagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gen09_provenance_tagged(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """GEN-09: each suggestion carries provenance='llm-proposed'."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    mock_llm_provider.chat = AsyncMock(
        return_value=(
            _make_llm_json([_suggestion("Child A"), _suggestion("Child B")]),
            100,
            50,
        )
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert all(s.provenance == "llm-proposed" for s in resp.suggestions), (
        "All LLM-generated suggestions must have provenance='llm-proposed'"
    )


# ---------------------------------------------------------------------------
# D-08: per-suggestion model + prompt-template provenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_and_prompt_template_provenance(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """Every generated suggestion carries provenance, 0-1 confidence, model id, and prompt key (D-08)."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    mock_llm_provider.chat = AsyncMock(
        return_value=(
            _make_llm_json(
                [_suggestion("Child A", confidence=0.9), _suggestion("Child B", confidence=85)]
            ),
            100,
            50,
        )
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
        model_id="gpt-4o",
    )

    assert len(resp.suggestions) == 2
    for s in resp.suggestions:
        assert s.provenance == "llm-proposed"
        assert s.confidence is not None and 0.0 <= s.confidence <= 1.0
        assert s.model == "gpt-4o"
        assert s.prompt_template == "children"


@pytest.mark.asyncio
async def test_model_provenance_none_when_model_id_omitted(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """model stays None when the caller does not thread a model_id; prompt key is still stamped."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    mock_llm_provider.chat = AsyncMock(
        return_value=(_make_llm_json([_suggestion("Sibling A")]), 100, 50)
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="siblings",
        batch_size=1,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert len(resp.suggestions) == 1
    assert resp.suggestions[0].model is None
    assert resp.suggestions[0].prompt_template == "siblings"


# ---------------------------------------------------------------------------
# D-05: batch_size configurable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_size_configurable(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """D-05: batch_size parameter controls the number requested in the prompt."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    captured_messages = []

    async def capture_chat(messages, **_kwargs):
        captured_messages.extend(messages)
        return (
            _make_llm_json(
                [_suggestion("Child A"), _suggestion("Child B"), _suggestion("Child C")]
            ),
            100,
            50,
        )

    mock_llm_provider.chat = capture_chat

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=3,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    full_text = " ".join(m["content"] for m in captured_messages)
    assert "3" in full_text, "batch_size=3 should appear in the prompt content"


# ---------------------------------------------------------------------------
# D-09: auto-validate in pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_validate_in_pipeline(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """D-09: pipeline runs validation + deduplication check per suggestion before returning results."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    mock_llm_provider.chat = AsyncMock(
        return_value=(
            _make_llm_json([_suggestion("Child A"), _suggestion("Child B")]),
            100,
            50,
        )
    )

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    # Every suggestion should have validation_errors list and duplicate_verdict
    for s in resp.suggestions:
        assert hasattr(s, "validation_errors"), "Suggestion missing validation_errors"
        assert isinstance(s.validation_errors, list), "validation_errors must be a list"
        assert hasattr(s, "duplicate_verdict"), "Suggestion missing duplicate_verdict"
        assert s.duplicate_verdict in ("pass", "warn", "block"), (
            f"Invalid verdict: {s.duplicate_verdict}"
        )

    # Validator was called once per suggestion
    assert mock_validator.validate_entity.call_count == 2
    # Dedup was called once per suggestion
    assert mock_duplicate_check_service.check.call_count == 2


@pytest.mark.asyncio
async def test_fail_soft_gates_emit_redacted_alert_events(
    mock_llm_provider,
    mock_duplicate_check_service,
    caplog: pytest.LogCaptureFixture,
):
    """Validation/dedup degradation stays visible without logging tenant data."""
    sensitive_label = "Confidential Matter\nforge=alert"
    sensitive_error = "provider response contained sk-live-secret"
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(side_effect=RuntimeError(sensitive_error))
    mock_duplicate_check_service.check = AsyncMock(side_effect=RuntimeError(sensitive_error))
    mock_llm_provider.chat = AsyncMock(
        return_value=(_make_llm_json([_suggestion(sensitive_label)]), 10, 5)
    )

    svc = _make_service(
        mock_llm_provider,
        mock_assembler,
        mock_validator,
        mock_duplicate_check_service,
    )
    with caplog.at_level("WARNING", logger="ontokit.services.suggestion_generation_service"):
        response = await svc.generate(
            project_id=PROJECT_ID,
            branch="main",
            class_iri=CLASS_IRI,
            suggestion_type="children",
            provider=mock_llm_provider,
            project_namespace=NAMESPACE,
        )

    assert response.suggestions[0].duplicate_verdict == "pass"
    assert "suggestion_validation_unavailable" in caplog.text
    assert "suggestion_dedup_unavailable" in caplog.text
    assert sensitive_label not in caplog.text
    assert sensitive_error not in caplog.text


# ---------------------------------------------------------------------------
# Pitfall 3: JSON parse handles markdown fences
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_parse_handles_markdown_fences(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """Pitfall 3: markdown code fences (```json ... ```) are stripped before JSON parsing."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    # Wrap the JSON in markdown fences as an LLM would often do
    fenced_json = f"```json\n{_make_llm_json([_suggestion('Child Fenced')])}\n```"
    mock_llm_provider.chat = AsyncMock(return_value=(fenced_json, 100, 50))

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert len(resp.suggestions) == 1, "Fenced JSON should be parsed correctly"
    assert resp.suggestions[0].label == "Child Fenced"


# ---------------------------------------------------------------------------
# Pitfall 4: confidence normalization scales
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_normalization_scales(
    mock_llm_provider,
    mock_duplicate_check_service,
):
    """Pitfall 4: confidence values >1.0 are divided by 100; string confidence returns None."""
    mock_assembler = AsyncMock()
    mock_assembler.assemble = AsyncMock(return_value=_make_context())
    mock_validator = AsyncMock()
    mock_validator.validate_entity = AsyncMock(return_value=[])

    suggs = [
        {"label": "No Confidence", "confidence": None},
        {"label": "String Confidence", "confidence": "high"},
        {"label": "100-scale Confidence", "confidence": 75},
        {"label": "Decimal Confidence", "confidence": 0.75},
    ]
    mock_llm_provider.chat = AsyncMock(return_value=(_make_llm_json(suggs), 100, 50))

    svc = _make_service(
        mock_llm_provider, mock_assembler, mock_validator, mock_duplicate_check_service
    )
    resp = await svc.generate(
        project_id=PROJECT_ID,
        branch="main",
        class_iri=CLASS_IRI,
        suggestion_type="children",
        batch_size=5,
        provider=mock_llm_provider,
        project_namespace=NAMESPACE,
    )

    assert len(resp.suggestions) == 4
    by_label = {s.label: s.confidence for s in resp.suggestions}

    assert by_label["No Confidence"] is None, "None confidence should remain None"
    assert by_label["String Confidence"] is None, "String confidence should be normalized to None"
    assert abs(by_label["100-scale Confidence"] - 0.75) < 1e-6, "75 should normalize to 0.75"
    assert abs(by_label["Decimal Confidence"] - 0.75) < 1e-6, "0.75 should remain 0.75"
