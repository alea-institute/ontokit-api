"""Quality filter to reject non-legal concepts from ontology expansion.

Ported from generative-folio/src/generative_folio/services/quality_filter.py.
Provides heuristic scoring as a FREE quality gate (no LLM cost) that runs
before duplicate detection or expensive LLM evaluation.

Functions adapted to accept plain strings/lists instead of ConceptGenerationOutput
since ontokit-api does not have that Pydantic model.

Architecture (heuristic-only here):
  1. Fast heuristic scoring (no LLM cost) catches obvious keeps/rejects.
  2. LLM-based check lives in the suggestion pipeline (not this module).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Heuristic constants (ported exactly from generative-folio quality_filter.py)
# ---------------------------------------------------------------------------

# Keywords whose presence in a definition signals legal relevance.
# Organized by category for maintainability.
LEGAL_DEFINITION_KEYWORDS: frozenset[str] = frozenset(
    {
        # Core legal vocabulary
        "law",
        "legal",
        "court",
        "statute",
        "regulation",
        "jurisdiction",
        "judicial",
        "adjudication",
        "tribunal",
        "legislation",
        "legislative",
        "enactment",
        # Parties and actors
        "plaintiff",
        "defendant",
        "litigant",
        "claimant",
        "petitioner",
        "respondent",
        "prosecutor",
        "counsel",
        "attorney",
        "judge",
        "magistrate",
        "arbitrator",
        "mediator",
        # Procedural
        "liability",
        "negligence",
        "tort",
        "contract",
        "breach",
        "remedy",
        "damages",
        "injunction",
        "restitution",
        "indemnity",
        "estoppel",
        "precedent",
        "appeal",
        "verdict",
        "judgment",
        "ruling",
        "conviction",
        "acquittal",
        "sentencing",
        "probation",
        "parole",
        # Rights and obligations
        "right",
        "obligation",
        "duty",
        "fiduciary",
        "due process",
        "constitutional",
        "amendment",
        "sovereignty",
        # Doctrines and principles
        "doctrine",
        "jurisprudence",
        "common law",
        "civil law",
        "equity",
        "mens rea",
        "actus reus",
        "locus standi",
        "habeas corpus",
        "stare decisis",
        "prima facie",
        # Regulatory
        "compliance",
        "regulatory",
        "enforcement",
        "sanction",
        "penalty",
        "prohibition",
        "license",
        "permit",
        # Property and transactions
        "property",
        "deed",
        "conveyance",
        "easement",
        "lien",
        "mortgage",
        "title",
        "encumbrance",
        # Criminal
        "criminal",
        "felony",
        "misdemeanor",
        "offense",
        "crime",
        "prosecution",
        # Corporate and commercial
        "corporate",
        "incorporation",
        "partnership",
        "fiduciary",
        "shareholder",
        "bankruptcy",
        "insolvency",
        # International
        "treaty",
        "convention",
        "international law",
        "diplomatic",
        "extradition",
    }
)

# Patterns that match common legal citation formats in sources.
LEGAL_SOURCE_PATTERNS: list[re.Pattern[str]] = [
    # U.S. Code: "42 U.S.C. S 1983", "26 USC 501"
    re.compile(r"\d+\s*U\.?S\.?C\.?\s*[S\u00a7]?\s*\d+", re.IGNORECASE),
    # Code of Federal Regulations: "17 C.F.R. 240"
    re.compile(r"\d+\s*C\.?F\.?R\.?\s*[S\u00a7]?\s*\d+", re.IGNORECASE),
    # Restatement references
    re.compile(r"Restatement\s*\(", re.IGNORECASE),
    # Case reporters: "410 U.S. 113", "347 F.3d 1024"
    re.compile(r"\d+\s+[A-Z][A-Za-z.]+\s+\d+"),
    # State statute patterns: "MCL 460.561", "Cal. Civ. Code"
    re.compile(r"[A-Z]{2,4}\.?\s+\d+\.\d+"),
    re.compile(r"[A-Z][a-z]+\.\s+[A-Z][a-z]+\.\s+Code", re.IGNORECASE),
    # Generic legal authority references
    re.compile(r"Black'?s\s+Law\s+Dictionary", re.IGNORECASE),
    re.compile(r"Uniform\s+Commercial\s+Code|U\.?C\.?C\.?", re.IGNORECASE),
    re.compile(r"Model\s+Penal\s+Code", re.IGNORECASE),
    re.compile(r"Federal\s+Rules?\s+of\s+Civil\s+Procedure", re.IGNORECASE),
    re.compile(r"Fed\.?\s*R\.?\s*(Civ|Crim|App|Evid)\.?\s*P\.?", re.IGNORECASE),
    # International: treaties, conventions
    re.compile(r"Convention\s+on\s+", re.IGNORECASE),
    re.compile(r"Treaty\s+of\s+", re.IGNORECASE),
    # EU regulations
    re.compile(r"Regulation\s*\(E[CU]\)\s*No\.?\s*\d+", re.IGNORECASE),
    re.compile(r"Directive\s+\d+/\d+/E[CU]", re.IGNORECASE),
]

# FOLIO's area-of-law classes (canonical names).
# A non-empty areas_of_law field that references these is a strong signal.
FOLIO_AREAS_OF_LAW: frozenset[str] = frozenset(
    {
        "Administrative Law",
        "Admiralty Law",
        "Antitrust Law",
        "Banking Law",
        "Bankruptcy Law",
        "Civil Procedure",
        "Commercial Law",
        "Constitutional Law",
        "Consumer Protection Law",
        "Contract Law",
        "Corporate Law",
        "Criminal Law",
        "Criminal Procedure",
        "Cybersecurity Law",
        "Education Law",
        "Election Law",
        "Employment Law",
        "Energy Law",
        "Environmental Law",
        "Evidence",
        "Family Law",
        "Health Law",
        "Immigration Law",
        "Insurance Law",
        "Intellectual Property Law",
        "International Law",
        "Labor Law",
        "Military Law",
        "Privacy Law",
        "Property Law",
        "Securities Law",
        "Tax Law",
        "Tort Law",
        "Transportation Law",
        "Trusts and Estates",
    }
)

# Thresholds for the heuristic score.
# Score ranges from 0.0 (clearly not legal) to 1.0 (clearly legal).
ACCEPT_THRESHOLD: float = 0.40
REJECT_THRESHOLD: float = 0.15


# ---------------------------------------------------------------------------
# Internal scoring helpers
# ---------------------------------------------------------------------------


def _count_definition_keyword_hits(definition: str) -> int:
    """Count how many distinct legal keywords appear in the definition."""
    definition_lower = definition.lower()
    hits = 0
    for keyword in LEGAL_DEFINITION_KEYWORDS:
        # Use word-boundary-ish matching: check that the keyword appears
        # as a substring (keywords like "common law" need substring match).
        if keyword in definition_lower:
            hits += 1
    return hits


def _count_source_pattern_hits(sources: list[str]) -> int:
    """Count how many sources match at least one legal citation pattern."""
    hits = 0
    for source in sources:
        for pattern in LEGAL_SOURCE_PATTERNS:
            if pattern.search(source):
                hits += 1
                break
    return hits


def _count_folio_area_matches(areas_of_law: list[str]) -> int:
    """Count how many areas_of_law match known FOLIO area-of-law classes."""
    # Normalize both sides for fuzzy matching.
    normalized_folio = {a.lower() for a in FOLIO_AREAS_OF_LAW}
    return sum(1 for a in areas_of_law if a.lower() in normalized_folio)


# ---------------------------------------------------------------------------
# Public scoring functions (adapted from generative-folio for plain dicts)
# ---------------------------------------------------------------------------


def compute_legal_score(
    definition: str,
    areas_of_law: list[str],
    sources: list[str],
    has_jurisdictions: bool,
    has_etymology: bool,
    has_notes: bool,
) -> float:
    """Compute a heuristic legal-relevance score in [0.0, 1.0].

    Ported from generative-folio quality_filter.compute_legal_score() with the
    signature adapted to accept plain strings/lists instead of
    ConceptGenerationOutput.

    The score is a weighted combination of four signals:

    1. areas_of_law coverage (0.35 weight):
       - Bonus for having recognized FOLIO areas of law.
       - Partial credit for any non-empty areas_of_law.

    2. Definition keyword density (0.30 weight):
       - How many legal keywords appear in the definition.

    3. Source quality (0.20 weight):
       - How many sources match legal citation patterns.

    4. Structural completeness (0.15 weight):
       - Having jurisdictions, sources, etymology, and notes.

    Args:
        definition: Legal definition text.
        areas_of_law: List of area-of-law strings.
        sources: List of source citation strings.
        has_jurisdictions: Whether the concept has jurisdiction metadata.
        has_etymology: Whether the concept has etymology metadata.
        has_notes: Whether the concept has notes metadata.

    Returns:
        Float between 0.0 and 1.0.
    """
    # --- Signal 1: areas_of_law (weight 0.35) ---
    folio_matches = _count_folio_area_matches(areas_of_law)
    n_areas = len(areas_of_law)
    if folio_matches >= 2:
        area_score = 1.0
    elif folio_matches == 1:
        area_score = 0.8
    elif n_areas >= 2:
        # Has areas but none match FOLIO exactly -- still some signal
        area_score = 0.4
    elif n_areas == 1:
        area_score = 0.3
    else:
        area_score = 0.0

    # --- Signal 2: definition keywords (weight 0.30) ---
    keyword_hits = _count_definition_keyword_hits(definition)
    if keyword_hits >= 5:
        keyword_score = 1.0
    elif keyword_hits >= 3:
        keyword_score = 0.8
    elif keyword_hits >= 2:
        keyword_score = 0.6
    elif keyword_hits >= 1:
        keyword_score = 0.3
    else:
        keyword_score = 0.0

    # --- Signal 3: source quality (weight 0.20) ---
    n_sources = len(sources)
    pattern_hits = _count_source_pattern_hits(sources)
    if pattern_hits >= 2:
        source_score = 1.0
    elif pattern_hits == 1:
        source_score = 0.7
    elif n_sources >= 2:
        # Has sources but no recognized patterns
        source_score = 0.3
    elif n_sources == 1:
        source_score = 0.2
    else:
        source_score = 0.0

    # --- Signal 4: structural completeness (weight 0.15) ---
    completeness_points = 0
    if has_jurisdictions:
        completeness_points += 1
    if sources:
        completeness_points += 1
    if has_etymology:
        completeness_points += 1
    if has_notes:
        completeness_points += 1
    completeness_score = min(completeness_points / 3.0, 1.0)

    # --- Weighted combination ---
    score = (
        0.35 * area_score
        + 0.30 * keyword_score
        + 0.20 * source_score
        + 0.15 * completeness_score
    )

    return round(score, 4)


def is_legal_concept(
    definition: str,
    areas_of_law: list[str],
    sources: list[str],
    has_jurisdictions: bool,
    has_etymology: bool,
    has_notes: bool,
) -> bool:
    """Determine whether a concept is legal enough to keep.

    Uses heuristic scoring only (no LLM cost). Applies a conservative
    threshold: concepts scoring at or above ACCEPT_THRESHOLD are accepted.

    Args:
        definition: Legal definition text.
        areas_of_law: List of area-of-law strings.
        sources: List of source citation strings.
        has_jurisdictions: Whether the concept has jurisdiction metadata.
        has_etymology: Whether the concept has etymology metadata.
        has_notes: Whether the concept has notes metadata.

    Returns:
        True if the concept scores >= ACCEPT_THRESHOLD.
    """
    score = compute_legal_score(
        definition=definition,
        areas_of_law=areas_of_law,
        sources=sources,
        has_jurisdictions=has_jurisdictions,
        has_etymology=has_etymology,
        has_notes=has_notes,
    )
    return score >= ACCEPT_THRESHOLD
