"""Annotations suggestion prompt template for ontology class suggestion generation.

Generates annotation suggestions (labels, examples, notes, translations) for a class.
Adapted from generative-folio translation.py TRANSLATION_SYSTEM_INSTRUCTIONS.
"""

from __future__ import annotations

# FOLIO target languages (BCP-47 tags) from generative-folio translation.py
_FOLIO_LANGUAGES = [
    ("de-de", "German (Germany)"),
    ("en-gb", "English (British)"),
    ("es-es", "Spanish (Spain)"),
    ("es-mx", "Spanish (Mexico)"),
    ("fr-fr", "French (France)"),
    ("he-il", "Hebrew (Israel)"),
    ("hi-in", "Hindi (India)"),
    ("ja-jp", "Japanese (Japan)"),
    ("pt-br", "Portuguese (Brazil)"),
    ("zh-cn", "Chinese (Simplified, China)"),
]

_LANG_LIST = "\n".join(f"  - {tag}: {name}" for tag, name in _FOLIO_LANGUAGES)

SYSTEM: str = (
    "You are a legal ontology enrichment specialist. "
    "Your task is to suggest annotations for the given ontology class. "
    "Annotations include:\n"
    "  - Alternative labels (skos:altLabel) — English synonyms and abbreviations\n"
    "  - Examples (skos:example) — concrete illustrative scenarios\n"
    "  - Notes (skos:note) — editorial or contextual notes\n"
    "  - Translations (skos:altLabel with BCP-47 lang tags) — use the standard legal term "
    "a legal professional in that jurisdiction would use in formal documents, statutes, "
    "or court proceedings — not a literal word-for-word translation.\n"
    f"FOLIO target languages (BCP-47 tag -> language):\n{_LANG_LIST}\n"
    "For languages with gendered forms, prefer the form most commonly used in legal texts. "
    "For CJK languages, use the standard legal term as found in codes, statutes, or "
    "authoritative legal dictionaries. "
    "For Hebrew, use modern legal Hebrew as used in Israeli statutes and court decisions. "
    "If a concept has no established equivalent in a target legal system, provide the closest "
    'functional equivalent with a brief gloss (e.g., "Fahrlässigkeit (negligence)"). '
    "Output JSON only. No markdown. No explanation outside the JSON structure. "
    'Output schema: {"suggestions": [{"property_iri": "string — e.g. skos:altLabel", '
    '"value": "string", "lang": "string|null — BCP-47 tag or null for English", '
    '"confidence": 0.0-1.0}]}'
)


def build_messages(context: dict, batch_size: int = 10) -> list[dict[str, str]]:
    """Build the messages list for LLMProvider.chat().

    Args:
        context: Dict from OntologyContextAssembler.assemble().
        batch_size: Number of annotation suggestions to request.

    Returns:
        List of {"role": str, "content": str} dicts (system + user).
    """
    cc = context["current_class"]
    label = (
        cc["labels"][0]["value"]
        if cc["labels"]
        else cc["iri"].rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    )

    annotations = cc.get("annotations", [])
    parents = context.get("parents", [])

    parts = [f"Ontology class: {label} <{cc['iri']}>"]

    if parents:
        parent_labels = ", ".join(p["label"] for p in parents)
        parts.append(f"Parent classes: {parent_labels}")

    if annotations:
        parts.append("Existing annotations:")
        for ann in annotations[:5]:
            prop_label = ann.get("property_label") or ann.get("property_iri", "")
            for val in ann.get("values", [])[:3]:
                lang_suffix = f" [{val.get('lang', '')}]" if val.get("lang") else ""
                parts.append(f"  {prop_label}: {val.get('value', '')}{lang_suffix}")

    parts.append(
        f"\nSuggest {batch_size} annotations for '{label}'. "
        "Include English synonyms, examples, notes, and multilingual translations "
        "in FOLIO's 10 target languages. "
        "Return JSON only."
    )

    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
