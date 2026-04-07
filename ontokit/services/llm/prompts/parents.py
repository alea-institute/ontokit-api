"""Parents suggestion prompt template for ontology class suggestion generation.

Generates additional parent class suggestions for a given ontology class.
Adapted from generative-folio qa/prompts.py ISA_VALIDATION_SYSTEM.
"""

from __future__ import annotations

# IS-A validation criteria adapted from generative-folio qa/prompts.py
_ISA_CRITERIA = (
    "A valid parent-child (IS-A) link means the child IS A TYPE/SUBCLASS of the parent. "
    "Examples of valid IS-A:\n"
    '  - "Gross Negligence" is-a "Negligence" → VALID\n'
    '  - "Contract Law" is-a "Area of Law" → VALID\n'
    "Examples of invalid IS-A:\n"
    '  - "Creative Commons License" is-a "Copyright Notice" → INVALID\n'
    '  - "Blockchain" is-a "Digital Signature" → INVALID\n'
    "Consider the concept's definition, areas of law, and the existing ontology hierarchy. "
    "Suggested parents must already exist in the ontology or be well-known OWL/FOLIO classes."
)

SYSTEM: str = (
    "You are a legal ontology taxonomy expert. "
    "Your task is to suggest additional parent classes for the given ontology class. "
    f"{_ISA_CRITERIA} "
    "Output JSON only. No markdown. No explanation outside the JSON structure. "
    'Output schema: {"suggestions": [{"label": "string — the parent class label", '
    '"iri": "string|null — IRI if known existing class", '
    '"definition": "string — why this IS-A relationship is valid", '
    '"confidence": 0.0-1.0}]}'
)


def build_messages(context: dict, batch_size: int = 3) -> list[dict[str, str]]:
    """Build the messages list for LLMProvider.chat().

    Args:
        context: Dict from OntologyContextAssembler.assemble().
        batch_size: Number of parent class suggestions to request.

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

    if cc.get("annotations"):
        for ann in cc["annotations"][:2]:
            prop_label = ann.get("property_label") or ann.get("property_iri", "")
            for val in ann.get("values", [])[:1]:
                parts.append(f"{prop_label}: {val.get('value', '')}")

    if parents:
        parent_labels = ", ".join(
            f"{p['label']} <{p['iri']}>" for p in parents
        )
        parts.append(f"Current parent classes: {parent_labels}")

    if siblings:
        sibling_labels = ", ".join(s["label"] for s in siblings[:8])
        parts.append(f"Sibling classes (for context): {sibling_labels}")

    parts.append(
        f"\nSuggest {batch_size} additional parent classes for '{label}'. "
        "Each must satisfy the IS-A relationship (child is a type/subclass of parent). "
        "Do NOT duplicate current parents. "
        "Return JSON only."
    )

    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
