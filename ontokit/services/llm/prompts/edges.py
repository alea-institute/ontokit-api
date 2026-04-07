"""Edges suggestion prompt template for ontology class suggestion generation.

Generates directed relationship (edge) suggestions from the given class to other classes.
Adapted from generative-folio concept_generation.py relations section (SYSTEM_INSTRUCTIONS).
"""

from __future__ import annotations

# Controlled relationship types adapted from generative-folio SYSTEM_INSTRUCTIONS
_RELATIONSHIP_TYPES = """Controlled relationship types (use ONLY these):
  - "seeAlso": general cross-reference to a related concept
  - "contrast": similar but legally distinct concept (e.g., negligence/recklessness)
  - "isGovernedBy": this concept is governed by a statute, regulation, or legal authority
    (e.g., "Contract Law" isGovernedBy "Uniform Commercial Code")
  - "supersedes": this concept replaces or supersedes another
    (e.g., "Model Penal Code" supersedes "Common Law Crimes")
  - "implements": this concept implements or effectuates another
    (e.g., "SEC Rule 10b-5" implements "Securities Exchange Act Section 10(b)")
  - "locatedIn": jurisdictional or geographic containment
    (e.g., "Delaware Court of Chancery" locatedIn "Delaware")
  - "appealsTo": appellate hierarchy
    (e.g., "U.S. District Court" appealsTo "U.S. Court of Appeals")
  - "isMemberOf": organizational membership
    (e.g., "Ninth Circuit" isMemberOf "U.S. Courts of Appeals")
  - "appendedTo": document or provision attached to another
  - "enables": this concept creates conditions for another
  - "requires": this concept depends on or requires another
  - "restricts": this concept limits or restricts another
  - "exemplifies": this concept exemplifies a broader pattern or principle
  - "hasSource": this concept derives from or originates in another concept
    (e.g., "Equity" hasSource "English Chancery Courts")
Do NOT use "sameAs". Prefer specific relationship types over "seeAlso" when applicable."""

SYSTEM: str = (
    "You are a legal ontology relationship expert. "
    "Your task is to suggest directed relationships (edges) from the given class "
    "to other classes in the ontology. "
    f"{_RELATIONSHIP_TYPES} "
    "Output JSON only. No markdown. No explanation outside the JSON structure. "
    'Output schema: {"suggestions": [{"target_label": "string", '
    '"target_iri": "string|null", '
    '"relationship_type": "string — one of the 14 controlled types", '
    '"explanation": "string — why this relationship exists", '
    '"confidence": 0.0-1.0}]}'
)


def build_messages(context: dict, batch_size: int = 5) -> list[dict[str, str]]:
    """Build the messages list for LLMProvider.chat().

    Args:
        context: Dict from OntologyContextAssembler.assemble().
        batch_size: Number of edge suggestions to request.

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
    annotations = cc.get("annotations", [])

    parts = [f"Ontology class: {label} <{cc['iri']}>"]

    if parents:
        parent_labels = ", ".join(p["label"] for p in parents)
        parts.append(f"Parent classes: {parent_labels}")

    # Include any existing relation annotations (seeAlso, isDefinedBy etc.)
    relation_iris = {
        "http://www.w3.org/2000/01/rdf-schema#seeAlso",
        "http://www.w3.org/2000/01/rdf-schema#isDefinedBy",
    }
    existing_relations = []
    for ann in annotations:
        if ann.get("property_iri") in relation_iris:
            for val in ann.get("values", [])[:3]:
                existing_relations.append(val.get("value", ""))

    if existing_relations:
        parts.append(f"Existing relations (do NOT duplicate): {', '.join(existing_relations)}")

    parts.append(
        f"\nSuggest {batch_size} directed relationships from '{label}' to other legal concepts. "
        "Use only the 14 controlled relationship types. "
        "Prefer specific types over seeAlso when applicable. "
        "Return JSON only."
    )

    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
