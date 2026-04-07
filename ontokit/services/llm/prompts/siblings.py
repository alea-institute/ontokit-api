"""Siblings suggestion prompt template for ontology class suggestion generation.

Generates sibling class (sharing the same parent) suggestions for a given class.
"""

from __future__ import annotations

SYSTEM: str = (
    "You are an ontology expert specializing in legal knowledge representation "
    "aligned with the FOLIO (Federated Open Legal Information Ontology) OWL/RDF/SKOS schema. "
    "Your task is to suggest sibling classes that share the same parent as the given ontology class. "
    "Each sibling must be a specific, non-overlapping legal concept at the same level of abstraction. "
    "Do NOT suggest concepts that are already listed as existing siblings. "
    "Every suggested sibling must have a precise legal definition distinguishing it from lay usage. "
    "Output JSON only. No markdown. No explanation outside the JSON structure. "
    'Output schema: {"suggestions": [{"label": "string", '
    '"definition": "string — one-sentence legal definition", '
    '"confidence": 0.0-1.0, "areas_of_law": ["string"]}]}'
)


def build_messages(context: dict, batch_size: int = 5) -> list[dict[str, str]]:
    """Build the messages list for LLMProvider.chat().

    Args:
        context: Dict from OntologyContextAssembler.assemble().
        batch_size: Number of sibling class suggestions to request.

    Returns:
        List of {"role": str, "content": str} dicts (system + user).
    """
    cc = context["current_class"]
    label = (
        cc["labels"][0]["value"]
        if cc["labels"]
        else cc["iri"].rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    )

    parents = context.get("parents", [])
    siblings = context.get("siblings", [])

    parts = [f"Ontology class: {label} <{cc['iri']}>"]

    if parents:
        parent_labels = ", ".join(p["label"] for p in parents)
        parts.append(f"Shared parent classes: {parent_labels}")

    if siblings:
        sibling_labels = ", ".join(s["label"] for s in siblings[:10])
        parts.append(f"Existing siblings (do NOT duplicate): {sibling_labels}")

    parts.append(
        f"\nSuggest {batch_size} new sibling classes for '{label}' "
        "(concepts sharing the same parent). "
        "Each must be at the same level of abstraction and not already listed above. "
        "Return JSON only."
    )

    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
