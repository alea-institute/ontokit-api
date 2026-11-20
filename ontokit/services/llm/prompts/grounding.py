"""Shared grounding lines for prompts that propose new classes."""

from __future__ import annotations

from typing import Any

from rdflib.namespace import SKOS

_MAX_DEFINITION_CHARS = 1000


def definition_line(current_class: dict[str, Any]) -> str | None:
    """Render the focus class's first skos:definition, or None when it has none.

    The value is capped so a long definition cannot push the request that
    follows it past the prompt's overall limit.
    """
    for annotation in current_class.get("annotations") or []:
        if annotation.get("property_iri") != str(SKOS.definition):
            continue
        for value in annotation.get("values") or []:
            text = " ".join(str(value.get("value") or "").split())
            if text:
                return f"Definition: {text[:_MAX_DEFINITION_CHARS]}"
    return None
