"""OWL reasoner service — wraps owlready2 for logical consistency checks."""

import logging
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ReasonerIssue(BaseModel):
    rule_id: str
    severity: str  # "error" | "warning"
    entity_iri: str | None = None
    message: str
    details: dict[str, Any] | None = None


class ReasonerResult(BaseModel):
    consistent: bool
    issues: list[ReasonerIssue]
    reasoner_used: str  # "owlready2" | "rdflib_fallback"


def _detect_cycles_rdflib(owl_content: str) -> list[ReasonerIssue]:
    """Detect cycles in subClassOf hierarchy using RDFLib graph traversal.

    Uses a DFS approach with path tracking — reliable and does not require
    owlready2 or Java runtime.
    """
    from rdflib import Graph, URIRef
    from rdflib.namespace import OWL, RDF, RDFS

    issues: list[ReasonerIssue] = []
    reported: set[str] = set()

    try:
        graph = Graph()
        # Auto-detect format: OWL/XML starts with <?xml or <rdf:RDF
        stripped = owl_content.lstrip()
        if stripped.startswith("<?xml") or stripped.startswith("<rdf:RDF") or stripped.startswith("<owl:"):
            graph.parse(data=owl_content, format="xml")
        else:
            graph.parse(data=owl_content, format="turtle")
    except Exception as e:
        logger.warning("RDFLib parse failed during cycle detection: %s", e)
        return issues

    # Collect all declared classes
    class_types = {OWL.Class, RDFS.Class}
    classes: set[URIRef] = set()
    for cls_type in class_types:
        for s in graph.subjects(RDF.type, cls_type):
            if isinstance(s, URIRef):
                classes.add(s)

    for cls in classes:
        # DFS with path tracking
        path: set[URIRef] = set()
        finished: set[URIRef] = set()
        stack: list[tuple[URIRef, bool]] = [(cls, False)]
        cycle_found = False

        while stack and not cycle_found:
            current, backtrack = stack.pop()
            if backtrack:
                path.discard(current)
                continue
            if current in path:
                iri = str(current)
                if iri not in reported:
                    issues.append(
                        ReasonerIssue(
                            rule_id="hierarchy_cycle",
                            severity="error",
                            entity_iri=iri,
                            message="Cycle detected in subClassOf hierarchy",
                        )
                    )
                    reported.add(iri)
                cycle_found = True
                continue
            if current in finished:
                continue
            path.add(current)
            stack.append((current, True))
            for parent in graph.objects(current, RDFS.subClassOf):
                if isinstance(parent, URIRef):
                    stack.append((parent, False))
            finished.add(current)

    return issues


class ReasonerService:
    """OWL 2 reasoning via owlready2 + RDFLib for consistency and cycle detection."""

    def check_consistency(self, owl_content: str) -> ReasonerResult:
        """Load OWL content and run consistency checking.

        Uses RDFLib for cycle detection (reliable, no Java dependency) and
        optionally owlready2 with HermiT for unsatisfiable class detection.

        Falls back to RDFLib-only checking if owlready2 fails.
        """
        issues: list[ReasonerIssue] = []

        # Always run RDFLib cycle detection (fast and reliable)
        cycle_issues = _detect_cycles_rdflib(owl_content)
        issues.extend(cycle_issues)

        # Run owlready2 HermiT for unsatisfiable class detection
        try:
            import owlready2

            # Write OWL content to temp file for owlready2
            with tempfile.NamedTemporaryFile(
                suffix=".owl", mode="w", delete=False, encoding="utf-8"
            ) as f:
                f.write(owl_content)
                temp_path = f.name

            try:
                onto = owlready2.get_ontology(f"file://{temp_path}").load()

                # Try running HermiT for unsatisfiable classes
                try:
                    with onto:
                        owlready2.sync_reasoner(infer_property_values=False)
                    # Check for unsatisfiable classes (Nothing subclasses)
                    nothing = owlready2.Nothing
                    for cls in onto.classes():
                        if nothing in cls.ancestors():
                            issues.append(
                                ReasonerIssue(
                                    rule_id="unsatisfiable_class",
                                    severity="error",
                                    entity_iri=cls.iri if hasattr(cls, "iri") else str(cls),
                                    message="Class is unsatisfiable (subclass of Nothing)",
                                )
                            )
                except Exception as e:
                    logger.warning("HermiT reasoner execution failed (non-fatal): %s", e)
                    issues.append(
                        ReasonerIssue(
                            rule_id="reasoner_warning",
                            severity="warning",
                            message=f"OWL reasoner could not complete: {e}",
                        )
                    )
            finally:
                Path(temp_path).unlink(missing_ok=True)

            return ReasonerResult(
                consistent=not any(i.severity == "error" for i in issues),
                issues=issues,
                reasoner_used="owlready2",
            )
        except ImportError:
            logger.warning("owlready2 not available — using RDFLib-only checks")
            return ReasonerResult(
                consistent=not any(i.severity == "error" for i in issues),
                issues=issues,
                reasoner_used="rdflib_fallback",
            )

    def _detect_cycles_owlready2(self, onto: Any) -> list[ReasonerIssue]:
        """Detect cycles in the subClassOf hierarchy using owlready2 class traversal.

        NOTE: This method is retained for API compatibility but cycle detection
        is now handled by _detect_cycles_rdflib() in check_consistency() which
        is more reliable since owlready2/HermiT may normalize cycles.
        """
        issues = []
        for cls in onto.classes():
            visited: set = set()
            current = cls
            path: list = [cls]
            while True:
                parents = list(current.is_a)
                owl_parents = [p for p in parents if hasattr(p, "iri") and p != current]
                found_cycle = False
                for parent in owl_parents:
                    if parent in visited:
                        cycle_iris = [str(getattr(c, "iri", c)) for c in path]
                        issues.append(
                            ReasonerIssue(
                                rule_id="hierarchy_cycle",
                                severity="error",
                                entity_iri=str(getattr(cls, "iri", cls)),
                                message="Cycle detected in class hierarchy",
                                details={"cycle_path": cycle_iris},
                            )
                        )
                        found_cycle = True
                        break
                    visited.add(parent)
                    path.append(parent)
                    current = parent
                if found_cycle or not owl_parents:
                    break
        return issues

    def detect_cycles(self, owl_content: str) -> list[ReasonerIssue]:
        """Detect hierarchy cycles only (lighter check than full consistency)."""
        return _detect_cycles_rdflib(owl_content)
