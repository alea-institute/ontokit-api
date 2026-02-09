"""Ontology linting service for checking ontology health."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from rdflib import Graph, Literal as RDFLiteral, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from app.models.lint import LintIssueType


@dataclass
class LintResult:
    """Result from a lint check."""

    issue_type: str  # error, warning, info
    rule_id: str
    message: str
    subject_iri: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class LintRuleInfo:
    """Information about a lint rule."""

    rule_id: str
    name: str
    description: str
    severity: str


# Available lint rules with their metadata
LINT_RULES: list[LintRuleInfo] = [
    LintRuleInfo(
        rule_id="missing-label",
        name="Missing Label",
        description="Classes should have at least one rdfs:label annotation",
        severity=LintIssueType.WARNING.value,
    ),
    LintRuleInfo(
        rule_id="missing-comment",
        name="Missing Comment",
        description="Classes should have a description via rdfs:comment",
        severity=LintIssueType.INFO.value,
    ),
    LintRuleInfo(
        rule_id="orphan-class",
        name="Orphan Class",
        description="Classes with no parent (other than owl:Thing) and no children may be misplaced",
        severity=LintIssueType.WARNING.value,
    ),
    LintRuleInfo(
        rule_id="undefined-parent",
        name="Undefined Parent",
        description="Class references a parent that is not defined in the ontology",
        severity=LintIssueType.ERROR.value,
    ),
    LintRuleInfo(
        rule_id="circular-hierarchy",
        name="Circular Hierarchy",
        description="Circular inheritance detected in class hierarchy",
        severity=LintIssueType.ERROR.value,
    ),
    LintRuleInfo(
        rule_id="empty-label",
        name="Empty Label",
        description="Class has a label that is an empty string",
        severity=LintIssueType.WARNING.value,
    ),
    LintRuleInfo(
        rule_id="duplicate-label",
        name="Duplicate Label",
        description="Multiple classes share the same label, which may cause confusion",
        severity=LintIssueType.WARNING.value,
    ),
]

# Map rule IDs to their info
LINT_RULES_MAP: dict[str, LintRuleInfo] = {rule.rule_id: rule for rule in LINT_RULES}


@dataclass
class OntologyLinter:
    """Service for checking ontology health and finding issues."""

    enabled_rules: set[str] = field(default_factory=lambda: {r.rule_id for r in LINT_RULES})

    def get_enabled_rules(self) -> list[LintRuleInfo]:
        """Get list of enabled lint rules."""
        return [r for r in LINT_RULES if r.rule_id in self.enabled_rules]

    async def lint(self, graph: Graph, project_id: UUID) -> list[LintResult]:
        """
        Run all enabled lint rules on the ontology graph.

        Args:
            graph: The RDF graph to lint
            project_id: The project ID (for context)

        Returns:
            List of lint results (issues found)
        """
        issues: list[LintResult] = []

        # Run each enabled rule
        for rule_id in self.enabled_rules:
            checker_name = f"_check_{rule_id.replace('-', '_')}"
            checker = getattr(self, checker_name, None)
            if checker:
                rule_issues = await checker(graph)
                issues.extend(rule_issues)

        return issues

    async def _check_missing_label(self, graph: Graph) -> list[LintResult]:
        """Find classes without rdfs:label."""
        issues = []

        for class_uri in graph.subjects(RDF.type, OWL.Class):
            if not isinstance(class_uri, URIRef):
                continue
            if class_uri == OWL.Thing:
                continue

            # Check for any rdfs:label
            labels = list(graph.objects(class_uri, RDFS.label))
            if not labels:
                issues.append(LintResult(
                    issue_type=LintIssueType.WARNING.value,
                    rule_id="missing-label",
                    message=f"Class has no rdfs:label annotation",
                    subject_iri=str(class_uri),
                    details={"local_name": self._get_local_name(class_uri)},
                ))

        return issues

    async def _check_missing_comment(self, graph: Graph) -> list[LintResult]:
        """Find classes without rdfs:comment."""
        issues = []

        for class_uri in graph.subjects(RDF.type, OWL.Class):
            if not isinstance(class_uri, URIRef):
                continue
            if class_uri == OWL.Thing:
                continue

            # Check for any rdfs:comment
            comments = list(graph.objects(class_uri, RDFS.comment))
            if not comments:
                # Get label for better context
                label = self._get_label(graph, class_uri)
                issues.append(LintResult(
                    issue_type=LintIssueType.INFO.value,
                    rule_id="missing-comment",
                    message=f"Class has no rdfs:comment description",
                    subject_iri=str(class_uri),
                    details={
                        "local_name": self._get_local_name(class_uri),
                        "label": label,
                    },
                ))

        return issues

    async def _check_orphan_class(self, graph: Graph) -> list[LintResult]:
        """Find classes with no parent (other than owl:Thing) and no children."""
        issues = []

        owl_thing = OWL.Thing

        for class_uri in graph.subjects(RDF.type, OWL.Class):
            if not isinstance(class_uri, URIRef):
                continue
            if class_uri == owl_thing:
                continue

            # Get parents (excluding owl:Thing)
            parents = [
                p for p in graph.objects(class_uri, RDFS.subClassOf)
                if isinstance(p, URIRef) and p != owl_thing
            ]

            # Get children
            children = list(graph.subjects(RDFS.subClassOf, class_uri))

            # Orphan if no meaningful parents and no children
            if not parents and not children:
                label = self._get_label(graph, class_uri)
                issues.append(LintResult(
                    issue_type=LintIssueType.WARNING.value,
                    rule_id="orphan-class",
                    message="Class has no parent classes and no children",
                    subject_iri=str(class_uri),
                    details={
                        "local_name": self._get_local_name(class_uri),
                        "label": label,
                    },
                ))

        return issues

    async def _check_undefined_parent(self, graph: Graph) -> list[LintResult]:
        """Find classes that reference undefined parent classes."""
        issues = []

        # Build set of all defined classes
        defined_classes = {
            str(c) for c in graph.subjects(RDF.type, OWL.Class)
            if isinstance(c, URIRef)
        }
        # Add owl:Thing as it's always implicitly defined
        defined_classes.add(str(OWL.Thing))

        for class_uri in graph.subjects(RDF.type, OWL.Class):
            if not isinstance(class_uri, URIRef):
                continue

            # Check each parent
            for parent_uri in graph.objects(class_uri, RDFS.subClassOf):
                if not isinstance(parent_uri, URIRef):
                    continue

                parent_str = str(parent_uri)
                if parent_str not in defined_classes:
                    label = self._get_label(graph, class_uri)
                    issues.append(LintResult(
                        issue_type=LintIssueType.ERROR.value,
                        rule_id="undefined-parent",
                        message=f"References undefined parent class",
                        subject_iri=str(class_uri),
                        details={
                            "local_name": self._get_local_name(class_uri),
                            "label": label,
                            "undefined_parent": parent_str,
                            "undefined_parent_local": self._get_local_name(parent_uri),
                        },
                    ))

        return issues

    async def _check_circular_hierarchy(self, graph: Graph) -> list[LintResult]:
        """Find circular inheritance in class hierarchy."""
        issues = []
        reported_cycles: set[frozenset[str]] = set()

        # Build adjacency list for subClassOf relationships
        subclass_of: dict[str, list[str]] = defaultdict(list)
        for class_uri in graph.subjects(RDF.type, OWL.Class):
            if isinstance(class_uri, URIRef):
                for parent in graph.objects(class_uri, RDFS.subClassOf):
                    if isinstance(parent, URIRef) and parent != OWL.Thing:
                        subclass_of[str(class_uri)].append(str(parent))

        def find_cycle(start: str, current: str, visited: set[str], path: list[str]) -> list[str] | None:
            """DFS to find cycle starting from 'start'."""
            if current in visited:
                if current == start:
                    return path
                return None

            visited.add(current)
            path.append(current)

            for parent in subclass_of.get(current, []):
                cycle = find_cycle(start, parent, visited, path)
                if cycle is not None:
                    return cycle

            path.pop()
            return None

        # Check each class for cycles
        for class_str in subclass_of.keys():
            cycle = find_cycle(class_str, class_str, set(), [])
            if cycle:
                # Create a frozenset to avoid reporting the same cycle multiple times
                cycle_set = frozenset(cycle)
                if cycle_set not in reported_cycles:
                    reported_cycles.add(cycle_set)
                    # Get labels for cycle classes
                    cycle_labels = [
                        self._get_label(graph, URIRef(c)) or self._get_local_name(URIRef(c))
                        for c in cycle
                    ]
                    issues.append(LintResult(
                        issue_type=LintIssueType.ERROR.value,
                        rule_id="circular-hierarchy",
                        message=f"Circular inheritance: {' → '.join(cycle_labels)} → {cycle_labels[0]}",
                        subject_iri=cycle[0],
                        details={
                            "cycle_iris": cycle,
                            "cycle_labels": cycle_labels,
                        },
                    ))

        return issues

    async def _check_empty_label(self, graph: Graph) -> list[LintResult]:
        """Find classes with empty string labels."""
        issues = []

        for class_uri in graph.subjects(RDF.type, OWL.Class):
            if not isinstance(class_uri, URIRef):
                continue
            if class_uri == OWL.Thing:
                continue

            # Check each label
            for label in graph.objects(class_uri, RDFS.label):
                if isinstance(label, RDFLiteral):
                    label_str = str(label).strip()
                    if not label_str:
                        issues.append(LintResult(
                            issue_type=LintIssueType.WARNING.value,
                            rule_id="empty-label",
                            message="Class has an empty rdfs:label",
                            subject_iri=str(class_uri),
                            details={
                                "local_name": self._get_local_name(class_uri),
                                "language": label.language,
                            },
                        ))

        return issues

    async def _check_duplicate_label(self, graph: Graph) -> list[LintResult]:
        """Find classes that share the same label."""
        issues = []

        # Build map of label → list of class IRIs
        label_to_classes: dict[str, list[str]] = defaultdict(list)

        for class_uri in graph.subjects(RDF.type, OWL.Class):
            if not isinstance(class_uri, URIRef):
                continue
            if class_uri == OWL.Thing:
                continue

            for label in graph.objects(class_uri, RDFS.label):
                if isinstance(label, RDFLiteral):
                    label_str = str(label).strip().lower()
                    if label_str:  # Skip empty labels
                        label_to_classes[label_str].append(str(class_uri))

        # Report duplicates
        reported_iris: set[str] = set()
        for label_str, class_iris in label_to_classes.items():
            if len(class_iris) > 1:
                for class_iri in class_iris:
                    if class_iri not in reported_iris:
                        reported_iris.add(class_iri)
                        # Get original (non-lowercased) label
                        original_label = self._get_label(graph, URIRef(class_iri))
                        other_classes = [c for c in class_iris if c != class_iri]
                        issues.append(LintResult(
                            issue_type=LintIssueType.WARNING.value,
                            rule_id="duplicate-label",
                            message=f"Label '{original_label}' is shared with {len(other_classes)} other class(es)",
                            subject_iri=class_iri,
                            details={
                                "local_name": self._get_local_name(URIRef(class_iri)),
                                "label": original_label,
                                "duplicate_iris": other_classes[:5],  # Limit to 5
                                "total_duplicates": len(other_classes),
                            },
                        ))

        return issues

    @staticmethod
    def _get_local_name(uri: URIRef) -> str:
        """Extract local name from IRI (after # or last /)."""
        iri = str(uri)
        if "#" in iri:
            return iri.split("#")[-1]
        return iri.rsplit("/", 1)[-1]

    @staticmethod
    def _get_label(graph: Graph, uri: URIRef) -> str | None:
        """Get the first rdfs:label for a URI."""
        for label in graph.objects(uri, RDFS.label):
            if isinstance(label, RDFLiteral):
                return str(label)
        return None


def get_linter(enabled_rules: set[str] | None = None) -> OntologyLinter:
    """
    Get an ontology linter instance.

    Args:
        enabled_rules: Set of rule IDs to enable. If None, all rules are enabled.

    Returns:
        Configured OntologyLinter instance
    """
    if enabled_rules is None:
        return OntologyLinter()
    return OntologyLinter(enabled_rules=enabled_rules)


def get_available_rules() -> list[LintRuleInfo]:
    """Get list of all available lint rules."""
    return LINT_RULES.copy()
