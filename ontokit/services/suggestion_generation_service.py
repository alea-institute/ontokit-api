"""SuggestionGenerationService — orchestrates the full suggestion generation pipeline.

Pipeline (per D-09 / RESEARCH.md Pattern 3):
  1. Assemble ontology context (GEN-06) via OntologyContextAssembler
  2. Build type-specific prompt (GEN-07) via PROMPT_BUILDERS dispatch
  3. Call LLM provider — returns (text, input_tokens, output_tokens)
  4. Parse JSON output — handles markdown code fences (Pitfall 3)
  5. Normalize confidence values — scales >1.0 by /100 (Pitfall 4 / GEN-08)
  6. Per-suggestion: mint IRI + validate + dedup — SEQUENTIAL to avoid AsyncSession
     concurrent use (Pitfall 5)
  7. Tag each suggestion provenance="llm-proposed" (GEN-09) plus model +
     prompt_template identity (D-08: metadata-only provenance)
  8. Return GenerateSuggestionsResponse with token counts for audit logging

Design notes:
  - AsyncSession is NOT safe for concurrent queries — steps 6 must be sequential.
  - LLM output is normalized from {suggestions: [...]} envelope or bare list.
  - Empty or malformed LLM output returns an empty suggestions list (not an error).
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.schemas.generation import (
    CONTROLLED_RELATIONSHIP_TYPES,
    GeneratedSuggestion,
    GenerateSuggestionsResponse,
    SuggestionType,
    ValidationError,
)
from ontokit.services.context_assembler import OntologyContextAssembler
from ontokit.services.duplicate_check_service import DuplicateCheckService
from ontokit.services.llm.base import LLMProvider
from ontokit.services.llm.prompts import PROMPT_BUILDERS
from ontokit.services.validation_service import ValidationService, mint_iri

logger = logging.getLogger(__name__)

_FORBIDDEN_IRI_CHARS = frozenset('<>"{}|\\^`')


def _is_safe_iri(value: str) -> bool:
    """Return whether a generated absolute IRI/CURIE is safe to serialize."""
    return bool(
        value
        and not any(char.isspace() or char in _FORBIDDEN_IRI_CHARS for char in value)
        and urlsplit(value).scheme
    )


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
        provider: LLMProvider | None = None,
        project_namespace: str = "",
        model_id: str | None = None,
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
            model_id:           Model identifier used for this call (e.g. "gpt-4o").
                                Stamped on each suggestion as provenance metadata
                                (D-08: metadata only — never raw prompt text).

        Returns:
            GenerateSuggestionsResponse with typed, validated suggestions plus
            token usage counts for cost audit logging.
        """
        if provider is None:
            raise ValueError("provider is required for suggestion generation")

        # ── Step 1: Assemble ontology context (GEN-06) ────────────────────────
        context = await self._assembler.assemble(project_id, branch, class_iri)

        # ── Step 2: Build type-specific prompt messages (GEN-07) ──────────────
        build_messages = PROMPT_BUILDERS[suggestion_type]
        messages = build_messages(context, batch_size)

        # ── Step 3: Call LLM ──────────────────────────────────────────────────
        text, input_tokens, output_tokens = await provider.chat(messages)

        # ── Step 4: Parse JSON output (handle Pitfall 3 — markdown fences) ────
        raw_suggestions = self._parse_json_safe(text)

        # ── Step 5 + 6 + 7: parse per-type, validate, dedup — SEQUENTIAL (Pitfall 5) ─
        # Each of the five suggestion types has a distinct LLM output schema and a
        # distinct ontology semantics, so parsing/validation/dedup must dispatch on
        # `suggestion_type` — a single generic `raw["label"]` parse silently drops
        # the payload of edges (target_*/relationship_type) and annotations
        # (property_iri/value/lang) and inverts the parent IS-A direction.
        results: list[GeneratedSuggestion] = []
        # Shared parent IRIs of the focus class (siblings share these; a sibling is
        # NOT a child of the focus class).
        shared_parent_iris = [
            p["iri"] for p in context.get("parents", []) if isinstance(p, dict) and p.get("iri")
        ]

        for raw in raw_suggestions:
            if not isinstance(raw, dict):
                continue
            parsed = self._parse_typed(
                raw, suggestion_type, class_iri, project_namespace, shared_parent_iris
            )
            if parsed is None:
                # Malformed / empty suggestion for this type — skip rather than
                # emit a blank, validation-failing stub.
                continue

            confidence = self._normalize_confidence(raw.get("confidence"))

            # Validate — only for class-like suggestions (children/siblings/parents).
            # Edges and annotations are not new subclasses, so the new-class rules
            # (VALID-01 parent-required etc.) don't apply; they carry their own
            # lightweight checks computed in _parse_typed.
            validation_errors = list(parsed["validation_errors"])
            if parsed["validate_entity"] is not None:
                try:
                    validation_errors += await self._validator.validate_entity(
                        project_id, branch, parsed["validate_entity"], project_namespace
                    )
                except Exception as exc:
                    logger.warning(
                        "Validation failed for %s suggestion %r: %s",
                        suggestion_type, parsed["label"], exc,
                    )

            # Duplicate check (D-09) — SEQUENTIAL, one await at a time
            duplicate_verdict = "pass"
            duplicate_candidates: list[dict[str, Any]] = []
            if parsed["dedup_label"]:
                try:
                    dedup_result = await self._dedup.check(
                        project_id,
                        label=parsed["dedup_label"],
                        parent_iri=parsed["dedup_parent"],
                    )
                    duplicate_verdict = dedup_result.verdict
                    duplicate_candidates = [
                        {"iri": c.iri, "label": c.label, "score": c.score}
                        for c in (dedup_result.candidates or [])
                    ]
                except Exception as exc:
                    # Dedup infra failure fails soft to "pass" — logged distinctly
                    # so ops can alert on silently-disabled duplicate blocking.
                    logger.warning(
                        "Dedup check unavailable for %s suggestion %r — allowing (verdict=pass): %s",
                        suggestion_type, parsed["dedup_label"], exc,
                    )

            # Build final suggestion (GEN-09: provenance="llm-proposed")
            results.append(
                GeneratedSuggestion(
                    iri=parsed["iri"],
                    suggestion_type=suggestion_type,
                    label=parsed["label"],
                    definition=raw.get("definition"),
                    confidence=confidence,
                    provenance="llm-proposed",
                    model=model_id,
                    prompt_template=suggestion_type,
                    validation_errors=validation_errors,
                    duplicate_verdict=duplicate_verdict,
                    duplicate_candidates=duplicate_candidates,
                    property_iri=parsed["property_iri"],
                    value=parsed["value"],
                    lang=parsed["lang"],
                    target_iri=parsed["target_iri"],
                    relationship_type=parsed["relationship_type"],
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
    def _parse_typed(
        raw: dict[str, Any],
        suggestion_type: SuggestionType,
        class_iri: str,
        project_namespace: str,
        shared_parent_iris: list[str],
    ) -> dict[str, Any] | None:
        """Parse one raw LLM suggestion into a typed payload for the focus class.

        Each suggestion type has its own LLM output schema (see prompts/*.py) and
        its own ontology semantics, so parsing dispatches on `suggestion_type`.
        Returns None when a required field for that type is missing/blank (so the
        pipeline skips it rather than emitting a blank, validation-failing stub).

        The returned dict is uniform:
          iri, label, definition-less; property_iri/value/lang/target_iri/
          relationship_type (payload); validate_entity (dict|None — run the
          new-class VALID-* rules only for class-like types); validation_errors
          (type-specific, pre-computed); dedup_label/dedup_parent (None ⇒ skip).
        """
        base: dict[str, Any] = {
            "property_iri": None,
            "value": None,
            "lang": None,
            "target_iri": None,
            "relationship_type": None,
            "validate_entity": None,
            "validation_errors": [],
            "dedup_label": None,
            "dedup_parent": None,
        }

        if suggestion_type in ("children", "siblings"):
            label = str(raw.get("label") or "").strip()
            if not label:
                return None
            new_iri = mint_iri(project_namespace)
            # A sibling shares the focus class's parents; a child is parented by
            # the focus class itself.
            parent_iris = (
                [class_iri]
                if suggestion_type == "children"
                else (shared_parent_iris or [class_iri])
            )
            return {
                **base,
                "iri": new_iri,
                "label": label,
                "validate_entity": {
                    "iri": new_iri,
                    "label": label,
                    "parent_iris": parent_iris,
                    "labels": [{"lang": "en", "value": label}],
                },
                "dedup_label": label,
                "dedup_parent": parent_iris[0] if parent_iris else class_iri,
            }

        if suggestion_type == "parents":
            label = str(raw.get("label") or "").strip()
            if not label:
                return None
            # The LLM may reference an existing parent by IRI — link to it rather
            # than minting a duplicate node. The IS-A direction (class_iri ⊑ parent)
            # is realized on accept by the web layer, which reads `iri` as the
            # parent to link.
            existing = str(raw.get("iri") or "").strip()
            if existing and not _is_safe_iri(existing):
                return None
            parent_iri = existing or mint_iri(project_namespace)
            return {
                **base,
                "iri": parent_iri,
                "label": label,
                # No new-class validation: a proposed parent may legitimately be a
                # root, so VALID-01 (parent-required) does not apply.
                "dedup_label": label,
                "dedup_parent": None,
            }

        if suggestion_type == "edges":
            target_label = str(raw.get("target_label") or "").strip()
            if not target_label:
                return None
            target_iri = str(raw.get("target_iri") or "").strip() or None
            rel = str(raw.get("relationship_type") or "").strip() or None
            errors: list[ValidationError] = []
            if rel is None or rel not in CONTROLLED_RELATIONSHIP_TYPES:
                errors.append(
                    ValidationError(
                        field="relationship_type",
                        code="GEN-05",
                        message=(
                            "relationship_type must be one of the "
                            f"{len(CONTROLLED_RELATIONSHIP_TYPES)} controlled types"
                        ),
                    )
                )
            if target_iri is None:
                errors.append(
                    ValidationError(
                        field="target_iri",
                        code="GEN-05",
                        message="edge target must reference an existing entity (target_iri was null)",
                    )
                )
            elif not _is_safe_iri(target_iri):
                errors.append(
                    ValidationError(
                        field="target_iri",
                        code="GEN-05",
                        message="edge target must be a safe absolute IRI",
                    )
                )
                target_iri = None
            return {
                **base,
                # The edge's identity is its target entity; fall back to a minted
                # IRI only so `iri` is never blank.
                "iri": target_iri or mint_iri(project_namespace),
                "label": target_label,
                "target_iri": target_iri,
                "relationship_type": rel,
                "validation_errors": errors,
            }

        if suggestion_type == "annotations":
            property_iri = str(raw.get("property_iri") or "").strip()
            value = str(raw.get("value") or "").strip()
            if not property_iri or not value:
                return None
            if not _is_safe_iri(property_iri):
                return None
            lang_raw = raw.get("lang")
            lang = str(lang_raw).strip() or None if lang_raw is not None else None
            return {
                **base,
                # An annotation is a property value ON the focus class, not a new
                # entity — so it carries the focus class's IRI.
                "iri": class_iri,
                "label": value,
                "property_iri": property_iri,
                "value": value,
                "lang": lang,
            }

        return None

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
                return cast(list[dict[str, Any]], data.get("suggestions", []))
            if isinstance(data, list):
                return cast(list[dict[str, Any]], data)
            return []
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
        if not isinstance(raw, (int, float, str)):
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None

        # NaN/inf are not valid confidences — clamping would silently promote NaN
        # to 1.0 (max confidence), so reject them outright.
        if not math.isfinite(val):
            return None

        if val > 1.0:
            val = val / 100.0
        return max(0.0, min(1.0, val))
