"""Children suggestion prompt template for ontology class suggestion generation.

Generates child class (rdfs:subClassOf) suggestions for a given ontology class.
Adapted from generative-folio concept_generation.py SYSTEM_INSTRUCTIONS.
"""

from __future__ import annotations

SYSTEM: str = (
    "You are an ontology expert specializing in legal knowledge representation "
    "aligned with the FOLIO (Federated Open Legal Information Ontology) OWL/RDF/SKOS schema. "
    "Your task is to suggest child classes (rdfs:subClassOf children) for the given ontology class. "
    "Each child must be a specific, non-redundant legal concept that IS-A subtype of the parent class. "
    "Do NOT suggest concepts that already exist as siblings or children. "
    "Every suggested child must have a precise legal definition distinguishing it from lay usage. "
    "Output JSON only. No markdown. No explanation outside the JSON structure. "
    'Output schema: {"suggestions": [{"label": "string", '
    '"definition": "string — one-sentence legal definition", '
    '"confidence": 0.0-1.0, "areas_of_law": ["string"]}]}'
)


def build_messages(context: dict, batch_size: int = 5) -> list[dict[str, str]]:
    """Build the messages list for LLMProvider.chat().

    Args:
        context: Dict from OntologyContextAssembler.assemble().
        batch_size: Number of child class suggestions to request.

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
    existing_children = context.get("existing_children", [])

    parts = [f"Ontology class: {label} <{cc['iri']}>"]

    if parents:
        parent_labels = ", ".join(p["label"] for p in parents)
        parts.append(f"Parent classes: {parent_labels}")

    if existing_children:
        child_labels = ", ".join(c["label"] for c in existing_children[:10])
        parts.append(f"Existing children (do NOT duplicate): {child_labels}")

    if siblings:
        sibling_labels = ", ".join(s["label"] for s in siblings[:10])
        parts.append(f"Sibling classes (for context): {sibling_labels}")

    parts.append(
        f"\nSuggest {batch_size} new child classes for '{label}'. "
        "Each must be a specific IS-A subtype, not already listed above. "
        "Return JSON only."
    )

    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
