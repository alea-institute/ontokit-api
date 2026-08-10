"""Hardened prompts and strict response parsers for ontology translation."""

from __future__ import annotations

import json
import math
from typing import Any

from ontokit.services.llm.prompts import harden_messages


def _messages(system: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    return harden_messages(
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]
    )


def build_translate_messages(
    source_literal: str,
    source_language: str,
    target_language: str,
    context_labels: list[str],
    *,
    include_confidence: bool = False,
) -> list[dict[str, str]]:
    """Build a translation request containing only the literal and allowed labels."""
    schema = '{"translation":"..."}'
    if include_confidence:
        schema = '{"translation":"...","confidence":0.0}'
    return _messages(
        "Translate the source literal faithfully. Return exactly one JSON object matching "
        f"{schema}; confidence, when requested, is a number from 0 to 1.",
        {
            "source_literal": source_literal,
            "source_language": source_language,
            "target_language": target_language,
            "context_labels": context_labels,
        },
    )


def build_back_translate_messages(
    translated_literal: str, source_language: str, translated_language: str
) -> list[dict[str, str]]:
    """Build a strict back-translation request."""
    return _messages(
        'Back-translate the literal. Return exactly {"translation":"..."}.',
        {
            "translated_literal": translated_literal,
            "translated_language": translated_language,
            "source_language": source_language,
        },
    )


def build_verify_messages(
    source_literal: str,
    source_language: str,
    target_language: str,
    primary_candidate: str,
    verifier_candidate: str,
    back_translation: str,
) -> list[dict[str, str]]:
    """Build an independent, normalized semantic-agreement request."""
    return _messages(
        "Judge semantic agreement, ignoring style differences. Return exactly "
        '{"agreement":0.0}, where agreement is a number from 0 to 1.',
        {
            "source_literal": source_literal,
            "source_language": source_language,
            "target_language": target_language,
            "primary_candidate": primary_candidate,
            "verifier_candidate": verifier_candidate,
            "back_translation": back_translation,
        },
    )


def _parse_object(text: str, required: frozenset[str], allowed: frozenset[str]) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("response must be exactly one JSON object") from exc
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise ValueError("response JSON has an invalid schema")
    return value


def _normalized_score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return score


def parse_translation_response(
    text: str, *, require_confidence: bool = False
) -> tuple[str, float | None]:
    allowed = frozenset({"translation", "confidence"})
    required = frozenset({"translation", "confidence"} if require_confidence else {"translation"})
    value = _parse_object(text, required, allowed)
    translation = value["translation"]
    if not isinstance(translation, str) or not translation.strip():
        raise ValueError("translation must be a non-empty string")
    confidence = value.get("confidence")
    return translation.strip(), None if confidence is None else _normalized_score(
        confidence, "confidence"
    )


def parse_verification_response(text: str) -> float:
    value = _parse_object(text, frozenset({"agreement"}), frozenset({"agreement"}))
    return _normalized_score(value["agreement"], "agreement")


__all__ = [
    "build_back_translate_messages",
    "build_translate_messages",
    "build_verify_messages",
    "parse_translation_response",
    "parse_verification_response",
]
