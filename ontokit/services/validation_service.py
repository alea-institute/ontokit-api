"""Validation service for pre-submit entity guardrails — Phase 13 Plan 01.

Implements the server-side gate (D-08) that blocks invalid entities before
they enter the draft pipeline. All 6 VALID-* rules are enforced here:

  VALID-01 — parent required (no free-floating entities)
  VALID-02 — English rdfs:label required
  VALID-03 — cycle detection (lightweight SQL ancestor path check)
  VALID-04 — namespace ownership (entity IRI must be in project namespace)
  VALID-05 — structured error returns (field + code + message)
  VALID-06 — IRI minting ({namespace}{uuid4} per D-11/D-12)

Design notes:
  - VALID-03 uses OntologyIndexService.get_ancestor_path() (SQL BFS/CTE),
    NOT the full RDFLib DFS in reasoner_service.py — per Pitfall 2 in
    RESEARCH.md: the SQL-based check is the lightweight gate here.
  - VALID-04 skips the check when entity IRI is empty/None (pre-mint state).
  - detect_project_namespace falls back to a stable fallback URL if the DB
    query yields no results.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.ontology_index import IndexedEntity
from ontokit.schemas.generation import ValidationError
from ontokit.services.ontology_index import OntologyIndexService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level utilities (VALID-06 / D-11 / D-12)
# ---------------------------------------------------------------------------


def mint_iri(namespace: str) -> str:
    """Mint a new IRI using UUID v4 local name under the given namespace.

    Ensures the namespace ends with '#' or '/'; appends '#' if neither.

    Per D-11: {namespace}{uuid4} — zero collision risk.
    Example: "http://example.org/ontology#a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    """
    if not namespace.endswith("#") and not namespace.endswith("/"):
        namespace = namespace + "#"
    local = str(uuid.uuid4())
    return f"{namespace}{local}"


def _extract_namespace(iri: str) -> str:
    """Extract namespace prefix from an IRI (text up to and including last # or /)."""
    if "#" in iri:
        return iri[: iri.rfind("#") + 1]
    if "/" in iri:
        return iri[: iri.rfind("/") + 1]
    return iri


async def detect_project_namespace(
    ontology_iri: str | None,
    db: AsyncSession,
    project_id: UUID,
    branch: str,
) -> str:
    """Detect or derive the canonical namespace for a project's ontology.

    Resolution order (D-12):
    1. Use provided ontology_iri (e.g., from owl:Ontology declaration)
    2. Query IndexedEntity for most common namespace prefix in the DB
    3. Fallback: "http://example.org/ontology/{project_id}#"

    The result always ends with '#' or '/'.
    """
    # 1. Provided IRI takes precedence
    if ontology_iri and ontology_iri.strip():
        ns = ontology_iri.strip()
        if not ns.endswith("#") and not ns.endswith("/"):
            ns = ns + "#"
        return ns

    # 2. Query DB for most common namespace prefix
    try:
        result = await db.execute(
            select(IndexedEntity.iri).where(
                IndexedEntity.project_id == project_id,
                IndexedEntity.branch == branch,
            ).limit(500)
        )
        iris = [row[0] for row in result.all()]
        if iris:
            ns_counts: dict[str, int] = {}
            for iri in iris:
                ns = _extract_namespace(iri)
                if ns and ns != iri:  # exclude IRIs with no separator
                    ns_counts[ns] = ns_counts.get(ns, 0) + 1
            if ns_counts:
                return max(ns_counts, key=lambda k: ns_counts[k])
    except Exception as exc:
        logger.warning("detect_project_namespace: DB query failed: %s", exc)

    # 3. Fallback
    return f"http://example.org/ontology/{project_id}#"


# ---------------------------------------------------------------------------
# ValidationService
# ---------------------------------------------------------------------------


class ValidationService:
    """Server-side entity validation gate (D-08).

    Runs all VALID-* rules and returns a combined list of structured errors.
    Called from the generation pipeline (D-09) and the standalone validate
    endpoint.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._index = OntologyIndexService(db)

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    async def validate_entity(
        self,
        project_id: UUID,
        branch: str,
        entity: dict[str, Any],
        project_namespace: str,
    ) -> list[ValidationError]:
        """Run all validation rules and return combined error list (VALID-05).

        Args:
            project_id:        UUID of the project being validated against.
            branch:            Branch name (e.g. "main").
            entity:            Dict with keys: label, parent_iris, labels, iri.
            project_namespace: Canonical namespace for IRI ownership check.

        Returns:
            List of ValidationError objects (empty = valid).
        """
        errors: list[ValidationError] = []

        errors.extend(self._check_parent_required(entity))
        errors.extend(self._check_english_label(entity))
        errors.extend(await self._check_cycles(entity, project_id, branch))
        errors.extend(self._check_namespace(entity, project_namespace))

        return errors

    # ──────────────────────────────────────────────────────────────────────
    # Validation rules
    # ──────────────────────────────────────────────────────────────────────

    def _check_parent_required(self, entity: dict[str, Any]) -> list[ValidationError]:
        """VALID-01: entity must have at least one parent class.

        New classes cannot be free-floating entities; every class must
        have at least one rdfs:subClassOf parent to anchor it in the hierarchy.
        """
        parent_iris = entity.get("parent_iris", [])
        if not parent_iris:
            return [
                ValidationError(
                    field="parent_iris",
                    code="VALID-01",
                    message=(
                        "At least one parent class is required. "
                        "New classes cannot be free-floating entities."
                    ),
                )
            ]
        return []

    def _check_english_label(self, entity: dict[str, Any]) -> list[ValidationError]:
        """VALID-02: entity must have at least one English rdfs:label.

        An English label (lang='en' or lang='' for language-untagged) is
        required so the entity is discoverable in the primary search index.
        """
        labels: list[dict] = entity.get("labels", [])
        has_english = any(
            label.get("lang") in ("en", "")
            for label in labels
            if isinstance(label, dict)
        )
        if not has_english:
            return [
                ValidationError(
                    field="labels",
                    code="VALID-02",
                    message=(
                        "An English rdfs:label is required. "
                        "Add a label with language tag 'en'."
                    ),
                )
            ]
        return []

    async def _check_cycles(
        self,
        entity: dict[str, Any],
        project_id: UUID,
        branch: str,
    ) -> list[ValidationError]:
        """VALID-03: detect would-be cycles in the class hierarchy.

        For each proposed parent IRI, query the SQL ancestor path of that parent.
        If the entity's own IRI appears in any ancestor path, assigning that
        parent would create a cycle.

        Uses OntologyIndexService.get_ancestor_path() — the lightweight SQL CTE
        check (Pitfall 2: NOT full OWL parse).
        """
        errors: list[ValidationError] = []
        entity_iri = entity.get("iri") or ""
        if not entity_iri:
            # IRI not yet minted → can't check cycles
            return []

        parent_iris: list[str] = entity.get("parent_iris", [])
        for parent_iri in parent_iris:
            try:
                ancestor_path = await self._index.get_ancestor_path(
                    project_id, branch, parent_iri
                )
                ancestor_iris = {node.get("iri") for node in ancestor_path if isinstance(node, dict)}
                if entity_iri in ancestor_iris:
                    errors.append(
                        ValidationError(
                            field="parent_iris",
                            code="VALID-03",
                            message=(
                                f"Assigning parent '{parent_iri}' would create a cycle: "
                                f"{entity_iri} is already an ancestor of {parent_iri}."
                            ),
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "VALID-03 cycle check failed for parent %s: %s", parent_iri, exc
                )
                # Fail open — don't block on DB errors (consistent with rate_limiter pattern)

        return errors

    def _check_namespace(
        self,
        entity: dict[str, Any],
        project_namespace: str,
    ) -> list[ValidationError]:
        """VALID-04: entity IRI must be in the project-owned namespace.

        Blocks IRIs that use namespaces belonging to other projects or
        external vocabularies. Skips the check when IRI is empty/None
        (pre-mint state — IRI will be assigned by mint_iri()).
        """
        entity_iri = entity.get("iri") or ""
        if not entity_iri:
            return []  # IRI not yet minted; skip check

        extracted_ns = _extract_namespace(entity_iri)
        if extracted_ns != project_namespace:
            return [
                ValidationError(
                    field="iri",
                    code="VALID-04",
                    message=(
                        f"IRI namespace '{extracted_ns}' is not owned by this project. "
                        f"Expected namespace: '{project_namespace}'."
                    ),
                )
            ]
        return []
