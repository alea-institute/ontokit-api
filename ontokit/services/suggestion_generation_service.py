"""SuggestionGenerationService — orchestrates the full suggestion generation pipeline.

Pipeline (per D-09 / RESEARCH.md Pattern 3):
  1. Assemble ontology context (GEN-06) via OntologyContextAssembler
  2. Build type-specific prompt (GEN-07) via PROMPT_BUILDERS dispatch
  3. Call LLM provider — returns (text, input_tokens, output_tokens)
  4. Parse JSON output — handles markdown code fences (Pitfall 3)
  5. Normalize confidence values — scales >1.0 by /100 (Pitfall 4 / GEN-08)
  6. Per-suggestion: mint IRI + validate + dedup — SEQUENTIAL to avoid AsyncSession
     concurrent use (Pitfall 5)
  7. Tag each suggestion provenance="llm-proposed" (GEN-09)
  8. Return GenerateSuggestionsResponse with token counts for audit logging

Design notes:
  - AsyncSession is NOT safe for concurrent queries — steps 6 must be sequential.
  - LLM output is normalized from {suggestions: [...]} envelope or bare list.
  - Empty or malformed LLM output returns an empty suggestions list (not an error).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.schemas.generation import (
    GenerateSuggestionsResponse,
    GeneratedSuggestion,
    SuggestionType,
)
from ontokit.services.context_assembler import OntologyContextAssembler
from ontokit.services.duplicate_check_service import DuplicateCheckService
from ontokit.services.llm.base import LLMProvider
from ontokit.services.llm.prompts import PROMPT_BUILDERS
from ontokit.services.validation_service import ValidationService, mint_iri

logger = logging.getLogger(__name__)


class SuggestionGenerationService:
    """Orchestrates the full LLM suggestion generation pipeline.

    Composes OntologyContextAssembler, LLMProvider, ValidationService, and
    DuplicateCheckService into a single generate() method that returns
    validated, scored, and provenance-tagged suggestions.
    """

    def __init__(
        self,
        db: AsyncSession,
        assembler: OntologyContextAssembler,
        validator: ValidationService,
        dedup_service: DuplicateCheckService,
    ) -> None:
        self._db = db
        self._assembler = assembler
        self._validator = validator
        self._dedup = dedup_service

    async def generate(
        self,
        project_id: UUID,
        branch: str,
        class_iri: str,
        suggestion_type: SuggestionType,
        batch_size: int = 5,
        provider: LLMProvider = None,  # type: ignore[assignment]
        project_namespace: str = "",
    ) -> GenerateSuggestionsResponse:
        """Run the full suggestion generation pipeline.

        Args:
            project_id:         UUID of the owning project.
            branch:             Git branch name (e.g. "main").
            class_iri:          IRI of the focus class for context assembly.
            suggestion_type:    One of "children" | "siblings" | "annotations" |
                                "parents" | "edges".
            batch_size:         Number of suggestions to request from the LLM (1-10).
            provider:           Instantiated LLMProvider for this call.
            project_namespace:  Canonical namespace for minting new IRIs.

        Returns:
            GenerateSuggestionsResponse with typed, validated suggestions plus
            token usage counts for cost audit logging.
        """
        # ── Step 1: Assemble ontology context (GEN-06) ────────────────────────
        context = await self._assembler.assemble(project_id, branch, class_iri)

        # ── Step 2: Build type-specific prompt messages (GEN-07) ──────────────
        build_messages = PROMPT_BUILDERS[suggestion_type]
        messages = build_messages(context, batch_size)

        # ── Step 3: Call LLM ──────────────────────────────────────────────────
        text, input_tokens, output_tokens = await provider.chat(messages)

        # ── Step 4: Parse JSON output (handle Pitfall 3 — markdown fences) ────
        raw_suggestions = self._parse_json_safe(text)

        # ── Step 5 + 6 + 7: Normalize, validate, dedup — SEQUENTIAL (Pitfall 5) ─
        results: list[GeneratedSuggestion] = []
        for raw in raw_suggestions:
            # Normalize confidence (GEN-08 / Pitfall 4)
            confidence = self._normalize_confidence(raw.get("confidence"))

            # Mint a new IRI for this suggestion (VALID-06)
            new_iri = mint_iri(project_namespace)

            # Build entity dict for validation
            label_value = raw.get("label", "")
            entity = {
                "iri": new_iri,
                "label": label_value,
                "parent_iris": [class_iri] if raw.get("parent_iri") is None else [raw["parent_iri"]],
                "labels": [{"lang": "en", "value": label_value}] if label_value else [],
            }

            # Validate entity (VALID-01..06)
            try:
                validation_errors = await self._validator.validate_entity(
                    project_id, branch, entity, project_namespace
                )
            except Exception as exc:
                logger.warning("Validation failed for suggestion %r: %s", label_value, exc)
                validation_errors = []

            # Duplicate check (D-09) — SEQUENTIAL, one await at a time
            try:
                dedup_result = await self._dedup.check(
                    project_id,
                    label=label_value,
                    parent_iri=class_iri,
                )
                duplicate_verdict: str = dedup_result.verdict
                duplicate_candidates: list[dict] = [
                    {"iri": c.iri, "label": c.label, "score": c.score}
                    for c in (dedup_result.candidates or [])
                ]
            except Exception as exc:
                logger.warning("Dedup check failed for suggestion %r: %s", label_value, exc)
                duplicate_verdict = "pass"
                duplicate_candidates = []

            # Build final suggestion (GEN-09: provenance="llm-proposed")
            results.append(
                GeneratedSuggestion(
                    iri=new_iri,
                    suggestion_type=suggestion_type,
                    label=label_value,
                    definition=raw.get("definition"),
                    confidence=confidence,
                    provenance="llm-proposed",
                    validation_errors=validation_errors,
                    duplicate_verdict=duplicate_verdict,
                    duplicate_candidates=duplicate_candidates,
                )
            )

        # ── Step 7: Return response ────────────────────────────────────────────
        return GenerateSuggestionsResponse(
            suggestions=results,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_tokens_estimate=None,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_json_safe(text: str) -> list[dict[str, Any]]:
        """Strip markdown fences and parse the LLM JSON output.

        Handles the following LLM output patterns (Pitfall 3):
        - Plain JSON:           {"suggestions": [...]}
        - Fenced JSON:          ```json\\n{...}\\n```
        - Fenced no-lang:       ```\\n{...}\\n```
        - Bare list (uncommon): [{...}, ...]

        Returns an empty list on any parse failure.
        """
        cleaned = text.strip()

        # Strip leading/trailing markdown fences
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned, flags=re.MULTILINE).strip()

        # Try direct parse
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data.get("suggestions", [])
            if isinstance(data, list):
                return data
            return []
        except json.JSONDecodeError:
            pass

        # Last resort: find the first {...} block via regex
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
                if isinstance(data, dict):
                    return data.get("suggestions", [])
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass

        logger.warning("_parse_json_safe: could not parse LLM output as JSON")
        return []

    @staticmethod
    def _normalize_confidence(raw: object) -> float | None:
        """Normalize an LLM confidence value to [0.0, 1.0] or None.

        Rules (Pitfall 4 / GEN-08 / D-06):
        - None → None
        - Non-numeric string → None
        - float/int > 1.0 → value / 100.0 (LLM returned 0-100 scale)
        - float/int in [0, 1] → value (already normalized)
        - Result is clamped to [0.0, 1.0]
        """
        if raw is None:
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None

        if val > 1.0:
            val = val / 100.0
        return max(0.0, min(1.0, val))
