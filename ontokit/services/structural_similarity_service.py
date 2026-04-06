"""Structural similarity via folio-python — parent/sibling Jaccard similarity."""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Module-level cache for FOLIO instances (per project_id or "default")
_folio_cache: dict[str, Any] = {}


def _get_folio_instance(project_key: str = "default") -> Any:
    """Get or create a cached FOLIO instance. Returns None if folio-python unavailable."""
    if project_key in _folio_cache:
        return _folio_cache[project_key]
    try:
        from folio.graph import FOLIO

        instance = FOLIO(use_cache=True)
        _folio_cache[project_key] = instance
        return instance
    except Exception:
        logger.warning("folio-python not available — structural similarity disabled")
        return None


def clear_folio_cache(project_key: str | None = None) -> None:
    """Clear cached FOLIO instance(s). Called on merge to force reload."""
    if project_key:
        _folio_cache.pop(project_key, None)
    else:
        _folio_cache.clear()


class StructuralSimilarityService:
    """Compute structural similarity between ontology entities using folio-python."""

    def compute_similarity(self, iri_a: str, iri_b: str, max_depth: int = 3) -> float:
        """Jaccard similarity between parent sets of two IRIs. Returns 0.0-1.0."""
        folio = _get_folio_instance()
        if folio is None:
            return 0.0
        try:
            parents_a = {c.iri for c in folio.get_parents(iri_a, max_depth=max_depth)}
            parents_b = {c.iri for c in folio.get_parents(iri_b, max_depth=max_depth)}
            if not parents_a and not parents_b:
                return 0.0
            intersection = parents_a & parents_b
            union = parents_a | parents_b
            return len(intersection) / len(union) if union else 0.0
        except Exception:
            logger.exception("Structural similarity failed for %s vs %s", iri_a, iri_b)
            return 0.0

    def get_structural_context(self, iri: str, max_depth: int = 3) -> dict:
        """Get parent and sibling IRIs for context. Returns {parents: [...], siblings: [...]}."""
        folio = _get_folio_instance()
        if folio is None:
            return {"parents": [], "siblings": []}
        try:
            parents = [
                {"iri": c.iri, "label": c.label}
                for c in folio.get_parents(iri, max_depth=max_depth)
            ]
            children = [
                {"iri": c.iri, "label": c.label}
                for c in folio.get_children(iri, max_depth=1)
            ]
            return {"parents": parents, "siblings": children}
        except Exception:
            logger.exception("Structural context failed for %s", iri)
            return {"parents": [], "siblings": []}

    def compute_best_structural_score(
        self, iri: str, candidate_iris: list[str], max_depth: int = 3
    ) -> dict[str, float]:
        """Compute structural similarity between iri and each candidate. Returns {candidate_iri: score}."""
        return {c: self.compute_similarity(iri, c, max_depth) for c in candidate_iris}
