"""Ontology service for managing OWL ontologies."""

from dataclasses import dataclass
from typing import Any, cast
from typing import Literal as TypingLiteral
from uuid import UUID

from rdflib import Graph, URIRef
from rdflib import Literal as RDFLiteral
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from ontokit.schemas.owl_class import (
    AnnotationProperty,
    EntitySearchResponse,
    EntitySearchResult,
    OWLClassResponse,
    OWLClassTreeNode,
)
from ontokit.services.storage import StorageService

# Map file extensions to RDF formats
FORMAT_MAP = {
    ".owl": "xml",
    ".owx": "xml",
    ".rdf": "xml",
    ".xml": "xml",
    ".ttl": "turtle",
    ".n3": "n3",
    ".nt": "nt",
    ".jsonld": "json-ld",
    ".json": "json-ld",
}

# Map prefix names to RDF properties
LABEL_PROPERTY_MAP = {
    "rdfs:label": RDFS.label,
    "skos:prefLabel": SKOS.prefLabel,
    "skos:altLabel": SKOS.altLabel,
    "skos:hiddenLabel": SKOS.hiddenLabel,
    "dcterms:title": URIRef("http://purl.org/dc/terms/title"),
    "dc:title": URIRef("http://purl.org/dc/elements/1.1/title"),
}

# Default label preferences if none specified
DEFAULT_LABEL_PREFERENCES = ["rdfs:label@en", "rdfs:label", "skos:prefLabel@en", "skos:prefLabel"]

# RDF types that make a subject a class. Mirrors the index's RDF_TYPE_MAP so the
# RDFLib (cold) path and the index (warm) path agree on which entities are classes.
CLASS_TYPES: tuple[URIRef, ...] = (OWL.Class, RDFS.Class)


def is_class(graph: Graph, subject: URIRef) -> bool:
    """Return True when ``subject`` is typed as ``owl:Class`` or ``rdfs:Class``."""
    return any((subject, RDF.type, class_type) in graph for class_type in CLASS_TYPES)


def iter_class_subjects(graph: Graph) -> list[URIRef]:
    """Return every class IRI (``owl:Class`` or ``rdfs:Class``) once, in first-seen order."""
    seen: set[URIRef] = set()
    classes: list[URIRef] = []
    for class_type in CLASS_TYPES:
        for subject in graph.subjects(RDF.type, class_type):
            if isinstance(subject, URIRef) and subject not in seen:
                seen.add(subject)
                classes.append(subject)
    return classes


# Common annotation properties to extract for class details
# (excludes rdfs:label and rdfs:comment which are handled separately)
ANNOTATION_PROPERTIES = {
    # Dublin Core Elements 1.1 (all 15 properties)
    "dc:contributor": URIRef("http://purl.org/dc/elements/1.1/contributor"),
    "dc:coverage": URIRef("http://purl.org/dc/elements/1.1/coverage"),
    "dc:creator": URIRef("http://purl.org/dc/elements/1.1/creator"),
    "dc:date": URIRef("http://purl.org/dc/elements/1.1/date"),
    "dc:description": URIRef("http://purl.org/dc/elements/1.1/description"),
    "dc:format": URIRef("http://purl.org/dc/elements/1.1/format"),
    "dc:identifier": URIRef("http://purl.org/dc/elements/1.1/identifier"),
    "dc:language": URIRef("http://purl.org/dc/elements/1.1/language"),
    "dc:publisher": URIRef("http://purl.org/dc/elements/1.1/publisher"),
    "dc:relation": URIRef("http://purl.org/dc/elements/1.1/relation"),
    "dc:rights": URIRef("http://purl.org/dc/elements/1.1/rights"),
    "dc:source": URIRef("http://purl.org/dc/elements/1.1/source"),
    "dc:subject": URIRef("http://purl.org/dc/elements/1.1/subject"),
    "dc:title": URIRef("http://purl.org/dc/elements/1.1/title"),
    "dc:type": URIRef("http://purl.org/dc/elements/1.1/type"),
    # Dublin Core Terms (commonly used)
    "dcterms:contributor": URIRef("http://purl.org/dc/terms/contributor"),
    "dcterms:coverage": URIRef("http://purl.org/dc/terms/coverage"),
    "dcterms:created": URIRef("http://purl.org/dc/terms/created"),
    "dcterms:creator": URIRef("http://purl.org/dc/terms/creator"),
    "dcterms:date": URIRef("http://purl.org/dc/terms/date"),
    "dcterms:description": URIRef("http://purl.org/dc/terms/description"),
    "dcterms:format": URIRef("http://purl.org/dc/terms/format"),
    "dcterms:identifier": URIRef("http://purl.org/dc/terms/identifier"),
    "dcterms:language": URIRef("http://purl.org/dc/terms/language"),
    "dcterms:modified": URIRef("http://purl.org/dc/terms/modified"),
    "dcterms:publisher": URIRef("http://purl.org/dc/terms/publisher"),
    "dcterms:relation": URIRef("http://purl.org/dc/terms/relation"),
    "dcterms:rights": URIRef("http://purl.org/dc/terms/rights"),
    "dcterms:source": URIRef("http://purl.org/dc/terms/source"),
    "dcterms:subject": URIRef("http://purl.org/dc/terms/subject"),
    "dcterms:title": URIRef("http://purl.org/dc/terms/title"),
    "dcterms:type": URIRef("http://purl.org/dc/terms/type"),
    # SKOS
    "skos:prefLabel": SKOS.prefLabel,
    "skos:altLabel": SKOS.altLabel,
    "skos:hiddenLabel": SKOS.hiddenLabel,
    "skos:definition": SKOS.definition,
    "skos:notation": SKOS.notation,
    "skos:example": SKOS.example,
    "skos:note": SKOS.note,
    "skos:scopeNote": SKOS.scopeNote,
    "skos:historyNote": SKOS.historyNote,
    "skos:editorialNote": SKOS.editorialNote,
    "skos:changeNote": SKOS.changeNote,
    "skos:related": SKOS.related,
    "skos:broader": SKOS.broader,
    "skos:narrower": SKOS.narrower,
    # Other common RDFS/OWL
    "rdfs:seeAlso": RDFS.seeAlso,
    "rdfs:isDefinedBy": RDFS.isDefinedBy,
}

_DEDICATED_CLASS_DETAIL_PROPERTIES = {RDFS.label, RDFS.comment}
_NON_ANNOTATION_PROPERTY_TYPES = {OWL.ObjectProperty, OWL.DatatypeProperty}


def annotation_property_label(property_iri: URIRef | str) -> str:
    """Return the configured compact label, or a stable local-name fallback."""
    iri = str(property_iri)
    for label, candidate in ANNOTATION_PROPERTIES.items():
        if str(candidate) == iri:
            return label
    if "#" in iri:
        return iri.rsplit("#", 1)[-1]
    return iri.rstrip("/").rsplit("/", 1)[-1]


def annotation_properties_for_graph(graph: Graph) -> dict[URIRef, str]:
    """Classify predicates that class detail should expose as annotations.

    The shared classification combines OntoKit's built-in annotation vocabulary
    with ontology-local ``owl:AnnotationProperty`` declarations. Predicates
    explicitly declared as object or datatype properties are never annotations,
    and fields represented elsewhere in class detail are excluded.
    """
    properties = {uri: label for label, uri in ANNOTATION_PROPERTIES.items()}
    declared = {
        subject
        for subject in graph.subjects(RDF.type, OWL.AnnotationProperty)
        if isinstance(subject, URIRef)
    }
    for property_uri in sorted(declared, key=str):
        properties.setdefault(property_uri, annotation_property_label(property_uri))

    non_annotation = {
        subject
        for property_type in _NON_ANNOTATION_PROPERTY_TYPES
        for subject in graph.subjects(RDF.type, property_type)
        if isinstance(subject, URIRef)
    }
    for property_uri in _DEDICATED_CLASS_DETAIL_PROPERTIES | non_annotation:
        properties.pop(property_uri, None)
    return properties


@dataclass
class LabelPreference:
    """Parsed label preference."""

    property_uri: URIRef
    language: str | None  # None means any language or no language tag

    @classmethod
    def parse(cls, pref_string: str) -> "LabelPreference | None":
        """
        Parse a preference string like 'rdfs:label@en' or 'skos:prefLabel'.

        Returns None if the property is not recognized.
        """
        if "@" in pref_string:
            prop_part, lang = pref_string.rsplit("@", 1)
        else:
            prop_part = pref_string
            lang = None

        prop_uri = LABEL_PROPERTY_MAP.get(prop_part)
        if prop_uri is None:
            return None

        return cls(property_uri=prop_uri, language=lang)


def select_preferred_label(
    graph: Graph,
    subject: URIRef,
    preferences: list[str] | None = None,
) -> str | None:
    """
    Select the best label for a subject based on preferences.

    Args:
        graph: The RDF graph
        subject: The subject to get a label for
        preferences: List of preference strings like ['rdfs:label@en', 'rdfs:label']

    Returns:
        The best matching label value, or None if no label found
    """
    prefs = preferences or DEFAULT_LABEL_PREFERENCES

    for pref_string in prefs:
        pref = LabelPreference.parse(pref_string)
        if pref is None:
            continue

        for obj in graph.objects(subject, pref.property_uri):
            if isinstance(obj, RDFLiteral):
                obj_lang = obj.language
                if pref.language is None:
                    # No language specified in preference - match any
                    return str(obj)
                elif pref.language == "" and obj_lang is None:
                    # Empty string means prefer no language tag
                    return str(obj)
                elif obj_lang == pref.language:
                    # Exact language match
                    return str(obj)

    # Fallback: return any label from rdfs:label
    for obj in graph.objects(subject, RDFS.label):
        if isinstance(obj, RDFLiteral):
            return str(obj)

    return None


class OntologyService:
    """Service for ontology CRUD operations."""

    def __init__(self, storage: StorageService | None = None) -> None:
        self._storage = storage
        self._graphs: dict[tuple[UUID, str], Graph] = {}

    async def serialize(
        self, ontology_id: UUID, format: str = "turtle", branch: str = "main"
    ) -> str:
        """Serialize ontology to string in specified format."""
        graph = await self._get_graph(ontology_id, branch)
        base = self._find_ontology_iri(graph)
        return graph.serialize(format=format, base=base)

    @staticmethod
    def _find_ontology_iri(graph: Graph) -> str | None:
        """Find the ontology IRI (subject of rdf:type owl:Ontology) for @base."""
        for subject in graph.subjects(RDF.type, OWL.Ontology):
            if isinstance(subject, URIRef):
                return str(subject)
        return None

    # Class operations

    async def get_class(
        self,
        ontology_id: UUID,
        class_iri: str,
        label_preferences: list[str] | None = None,
        branch: str = "main",
    ) -> OWLClassResponse | None:
        """Get a class by IRI."""
        graph = await self._get_graph(ontology_id, branch)
        class_uri = URIRef(class_iri)

        if (class_uri, RDF.type, OWL.Class) not in graph:
            return None

        return await self._class_to_response(graph, class_uri, label_preferences)

    async def get_root_classes(
        self,
        project_id: UUID,
        label_preferences: list[str] | None = None,
        branch: str = "main",
    ) -> list[OWLClassResponse]:
        """
        Get all root classes (classes with no parent or only owl:Thing as parent).

        These are the top-level classes in the ontology hierarchy.
        """
        graph = await self._get_graph(project_id, branch)
        root_classes = []

        owl_thing = OWL.Thing

        for class_uri in iter_class_subjects(graph):
            # Skip owl:Thing itself
            if class_uri == owl_thing:
                continue

            # Get all parents
            parents = [
                p for p in graph.objects(class_uri, RDFS.subClassOf) if isinstance(p, URIRef)
            ]

            # Check if this is a root class:
            # - No parents, or
            # - Only parent is owl:Thing
            is_root = len(parents) == 0 or (len(parents) == 1 and parents[0] == owl_thing)

            if is_root:
                root_classes.append(
                    await self._class_to_response(graph, class_uri, label_preferences)
                )

        # Sort by label (or IRI if no label)
        def sort_key(cls: OWLClassResponse) -> str:
            if cls.labels:
                return cls.labels[0].value.lower()
            return str(cls.iri).lower()

        root_classes.sort(key=sort_key)
        return root_classes

    async def get_class_children(
        self,
        project_id: UUID,
        class_iri: str,
        label_preferences: list[str] | None = None,
        branch: str = "main",
    ) -> list[OWLClassResponse]:
        """
        Get direct children of a class (classes that have this class as a direct parent).
        """
        graph = await self._get_graph(project_id, branch)
        parent_uri = URIRef(class_iri)
        children = []

        for class_uri in graph.subjects(RDFS.subClassOf, parent_uri):
            if not isinstance(class_uri, URIRef):
                continue
            if not is_class(graph, class_uri):
                continue
            children.append(await self._class_to_response(graph, class_uri, label_preferences))

        # Sort by label (or IRI if no label)
        def sort_key(cls: OWLClassResponse) -> str:
            if cls.labels:
                return cls.labels[0].value.lower()
            return str(cls.iri).lower()

        children.sort(key=sort_key)
        return children

    async def get_class_count(self, project_id: UUID, branch: str = "main") -> int:
        """Get total number of classes in the ontology."""
        graph = await self._get_graph(project_id, branch)
        return sum(1 for s in iter_class_subjects(graph) if s != OWL.Thing)

    async def search_entities(
        self,
        project_id: UUID,
        query: str,
        entity_types: list[str] | None = None,
        label_preferences: list[str] | None = None,
        limit: int = 50,
        branch: str = "main",
    ) -> EntitySearchResponse:
        """
        Search for entities (classes, properties, individuals) by name.

        Matches against labels, local names, and full IRIs (case-insensitive substring).
        """
        graph = await self._get_graph(project_id, branch)
        query_lower = query.lower()

        # Map entity type names to (RDF type, result entity_type, property_kind).
        # property_kind preserves the OWL subtype so clients can categorize
        # properties without IRI-substring guesses (see issue #117). A bare
        # rdf:Property has no OWL kind, so its property_kind is None. The OWL
        # types come first so a dual-typed entity is reported with its OWL kind;
        # this order mirrors the index's RDF_TYPE_MAP.
        type_mapping: dict[str, list[tuple[URIRef, str, str | None]]] = {
            "class": [(OWL.Class, "class", None), (RDFS.Class, "class", None)],
            "property": [
                (OWL.ObjectProperty, "property", "object"),
                (OWL.DatatypeProperty, "property", "data"),
                (OWL.AnnotationProperty, "property", "annotation"),
                (RDF.Property, "property", None),
            ],
            "individual": [(OWL.NamedIndividual, "individual", None)],
        }

        allowed_types = entity_types or ["class", "property", "individual"]
        rdf_types: list[tuple[URIRef, str, str | None]] = []
        for t in allowed_types:
            if t in type_mapping:
                rdf_types.extend(type_mapping[t])

        owl_thing = OWL.Thing
        results: list[EntitySearchResult] = []
        # An entity typed several ways within one result kind (e.g. owl:Class and
        # rdfs:Class, or rdf:Property and owl:ObjectProperty) is reported once,
        # with the first (OWL) type in type_mapping order.
        seen: set[tuple[str, URIRef]] = set()

        for rdf_type, entity_type, property_kind in rdf_types:
            for subject in graph.subjects(RDF.type, rdf_type):
                if not isinstance(subject, URIRef):
                    continue
                if subject == owl_thing:
                    continue
                if (entity_type, subject) in seen:
                    continue
                seen.add((entity_type, subject))

                iri_str = str(subject)

                # Extract local name
                if "#" in iri_str:
                    local_name = iri_str.split("#")[-1]
                else:
                    local_name = iri_str.rsplit("/", 1)[-1]

                # Collect all label values for matching
                all_labels: list[str] = []
                for label_prop in LABEL_PROPERTY_MAP.values():
                    for obj in graph.objects(subject, label_prop):
                        if isinstance(obj, RDFLiteral):
                            all_labels.append(str(obj))

                # Check for match ("*" matches everything)
                if query_lower != "*":
                    matched = (
                        query_lower in local_name.lower()
                        or query_lower in iri_str.lower()
                        or any(query_lower in lbl.lower() for lbl in all_labels)
                    )

                    if not matched:
                        continue

                # Resolve display label
                display_label = select_preferred_label(graph, subject, label_preferences)
                if not display_label:
                    display_label = local_name

                # Check deprecated status
                deprecated = False
                for obj in graph.objects(subject, OWL.deprecated):
                    if str(obj).lower() in ("true", "1"):
                        deprecated = True
                        break

                results.append(
                    EntitySearchResult(
                        iri=iri_str,
                        label=display_label,
                        entity_type=cast(
                            TypingLiteral["class", "property", "individual"], entity_type
                        ),
                        property_kind=cast(
                            TypingLiteral["object", "data", "annotation"] | None,
                            property_kind,
                        ),
                        deprecated=deprecated,
                    )
                )

        # Sort: exact prefix matches on label first, then alphabetical
        def sort_key(r: EntitySearchResult) -> tuple[int, str]:
            label_lower = r.label.lower()
            if label_lower.startswith(query_lower):
                return (0, label_lower)
            return (1, label_lower)

        results.sort(key=sort_key)
        total = len(results)
        results = results[:limit]

        return EntitySearchResponse(results=results, total=total)

    async def get_ancestor_path(
        self,
        project_id: UUID,
        class_iri: str,
        label_preferences: list[str] | None = None,
        branch: str = "main",
    ) -> list[OWLClassTreeNode]:
        """
        Get the path from root to a specific class.

        Returns a list of tree nodes starting from the root class down to
        (but not including) the target class. This is useful for expanding
        the tree to reveal a specific class.

        Returns an empty list if the class is a root class or not found.
        """
        graph = await self._get_graph(project_id, branch)
        target_uri = URIRef(class_iri)
        owl_thing = OWL.Thing

        # Check if target class exists
        if not is_class(graph, target_uri):
            return []

        # Build ancestor path by traversing upward
        path: list[URIRef] = []
        visited: set[str] = set()
        current = target_uri

        while True:
            if str(current) in visited:
                # Circular hierarchy - break
                break
            visited.add(str(current))

            # Get parents
            parents = [
                p
                for p in graph.objects(current, RDFS.subClassOf)
                if isinstance(p, URIRef) and p != owl_thing
            ]

            if not parents:
                # Reached a root class
                break

            # Use first parent for the path (in complex hierarchies,
            # a class might have multiple parents - we pick one path)
            parent = parents[0]
            path.append(parent)
            current = parent

        # Reverse to get root-to-target order
        path.reverse()

        # Convert to tree nodes
        result = []
        for uri in path:
            response = await self._class_to_response(graph, uri, label_preferences)
            result.append(self._class_to_tree_node(response, label_preferences))

        return result

    async def get_root_tree_nodes(
        self,
        project_id: UUID,
        label_preferences: list[str] | None = None,
        branch: str = "main",
    ) -> list[OWLClassTreeNode]:
        """Get root classes as tree nodes (optimized for tree view)."""
        root_classes = await self.get_root_classes(project_id, label_preferences, branch)
        return [self._class_to_tree_node(cls, label_preferences) for cls in root_classes]

    async def get_children_tree_nodes(
        self,
        project_id: UUID,
        class_iri: str,
        label_preferences: list[str] | None = None,
        branch: str = "main",
    ) -> list[OWLClassTreeNode]:
        """Get children of a class as tree nodes (optimized for tree view)."""
        children = await self.get_class_children(project_id, class_iri, label_preferences, branch)
        return [self._class_to_tree_node(cls, label_preferences) for cls in children]

    def _class_to_tree_node(
        self,
        cls: OWLClassResponse,
        label_preferences: list[str] | None = None,  # noqa: ARG002
    ) -> OWLClassTreeNode:
        """Convert an OWLClassResponse to a tree node."""
        # The preferred label should already be computed during _class_to_response
        # For tree nodes, we want a single label - use first from labels list
        # which should be ordered by preference
        if cls.labels:
            label = cls.labels[0].value
        else:
            # Extract local name from IRI (after # or last /)
            iri = str(cls.iri)
            label = iri.split("#")[-1] if "#" in iri else iri.rsplit("/", 1)[-1]

        return OWLClassTreeNode(
            iri=str(cls.iri),
            label=label,
            child_count=cls.child_count,
            deprecated=cls.deprecated,
        )

    # Helper methods

    async def load_from_storage(
        self, project_id: UUID, source_file_path: str, branch: str = "main"
    ) -> Graph:
        """
        Load an ontology from MinIO storage.

        Args:
            project_id: The project UUID (used for caching)
            source_file_path: The full path in MinIO (e.g., "ontokit/projects/{id}/ontology.owl")
            branch: The branch name (used for cache key)

        Returns:
            The parsed RDF graph

        Raises:
            StorageError: If the file cannot be loaded
            ValueError: If the file format is not supported
        """
        if self._storage is None:
            raise ValueError("Storage service not configured")

        # Extract the object name (remove bucket prefix if present)
        # source_file_path format: "ontokit/projects/{id}/ontology.owl" or "projects/{id}/ontology.owl"
        parts = source_file_path.split("/", 1)
        if len(parts) == 2 and parts[0] == self._storage.bucket:
            object_name = parts[1]
        else:
            object_name = source_file_path

        # Determine format from file extension
        ext = "." + object_name.rsplit(".", 1)[-1].lower() if "." in object_name else ""
        rdf_format = FORMAT_MAP.get(ext)
        if not rdf_format:
            raise ValueError(f"Unsupported file format: {ext}")

        # Download and parse
        content = await self._storage.download_file(object_name)
        graph = Graph()
        graph.parse(data=content.decode("utf-8"), format=rdf_format)

        # Cache the graph with branch key
        self._graphs[(project_id, branch)] = graph
        return graph

    async def load_from_git(
        self,
        project_id: UUID,
        branch: str,
        filename: str,
        git_service: Any,
    ) -> Graph:
        """
        Load an ontology from a git branch.

        Args:
            project_id: The project UUID
            branch: The branch name to read from
            filename: The ontology filename (e.g., "ontology.ttl")
            git_service: GitRepositoryService instance

        Returns:
            The parsed RDF graph
        """
        # Determine format from file extension
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        rdf_format = FORMAT_MAP.get(ext)
        if not rdf_format:
            raise ValueError(f"Unsupported file format: {ext}")

        # Read file content from git branch
        content = git_service.get_file_from_branch(project_id, branch, filename)

        graph = Graph()
        graph.parse(data=content.decode("utf-8"), format=rdf_format)

        # Cache with branch key
        self._graphs[(project_id, branch)] = graph
        return graph

    async def _get_graph(self, ontology_id: UUID, branch: str = "main") -> Graph:
        """Get the cached RDF graph for a project and branch."""
        key = (ontology_id, branch)
        if key not in self._graphs:
            raise ValueError(
                f"Graph for project {ontology_id} branch {branch} not loaded. "
                "Call load_from_storage or load_from_git first."
            )
        return self._graphs[key]

    def set_graph(self, project_id: UUID, branch: str, graph: Graph) -> None:
        """Inject a pre-parsed graph into the cache (for testing/benchmarks)."""
        self._graphs[(project_id, branch)] = graph

    async def get_graph(self, ontology_id: UUID, branch: str = "main") -> Graph:
        """Get the cached RDF graph for a project and branch.

        Raises ``ValueError`` if the graph has not been loaded yet.
        """
        return await self._get_graph(ontology_id, branch)

    def is_loaded(self, project_id: UUID, branch: str = "main") -> bool:
        """Check if a project's ontology graph is loaded in memory for a given branch."""
        return (project_id, branch) in self._graphs

    def unload(self, project_id: UUID, branch: str | None = None) -> None:
        """Remove a project's ontology graph from memory.

        If branch is None, remove all cached graphs for the project.
        Otherwise, remove only the specified branch's graph.
        """
        if branch is None:
            keys_to_remove = [k for k in self._graphs if k[0] == project_id]
            for k in keys_to_remove:
                del self._graphs[k]
        else:
            self._graphs.pop((project_id, branch), None)

    async def _class_to_response(
        self,
        graph: Graph,
        class_uri: URIRef,
        label_preferences: list[str] | None = None,
    ) -> OWLClassResponse:
        """Convert a class URI to response schema."""
        from ontokit.schemas.ontology import LocalizedString

        labels = [
            LocalizedString(value=str(label), lang=label.language or "en")
            for label in graph.objects(class_uri, RDFS.label)
            if isinstance(label, RDFLiteral)
        ]

        comments = [
            LocalizedString(value=str(comment), lang=comment.language or "en")
            for comment in graph.objects(class_uri, RDFS.comment)
            if isinstance(comment, RDFLiteral)
        ]

        parent_iris = [
            str(p) for p in graph.objects(class_uri, RDFS.subClassOf) if isinstance(p, URIRef)
        ]

        # Resolve labels for parent classes
        parent_labels: dict[str, str] = {}
        for parent_iri in parent_iris:
            parent_uri = URIRef(parent_iri)
            label = select_preferred_label(graph, parent_uri, label_preferences)
            if label:
                parent_labels[parent_iri] = label
            else:
                # Fall back to local name
                if "#" in parent_iri:
                    parent_labels[parent_iri] = parent_iri.split("#")[-1]
                else:
                    parent_labels[parent_iri] = parent_iri.rsplit("/", 1)[-1]

        # Count direct children (classes that have this class as a parent)
        child_count = sum(
            1
            for _ in graph.subjects(RDFS.subClassOf, class_uri)
            if isinstance(_, URIRef) and is_class(graph, _)
        )

        # Check for deprecated annotation (owl:deprecated = true)
        deprecated = False
        for obj in graph.objects(class_uri, OWL.deprecated):
            if str(obj).lower() in ("true", "1"):
                deprecated = True
                break

        # Count instances (individuals of this class)
        instance_count = sum(
            1 for _ in graph.subjects(RDF.type, class_uri) if isinstance(_, URIRef)
        )

        # Extract additional annotation properties (DC, SKOS, and ontology-local
        # owl:AnnotationProperty declarations).
        annotations = []
        for prop_uri, prop_label in annotation_properties_for_graph(graph).items():
            values = []
            for obj in graph.objects(class_uri, prop_uri):
                if isinstance(obj, RDFLiteral):
                    values.append(LocalizedString(value=str(obj), lang=obj.language or ""))
                elif isinstance(obj, URIRef):
                    # For URI values, store as string with empty lang
                    values.append(LocalizedString(value=str(obj), lang=""))
            if values:
                annotations.append(
                    AnnotationProperty(
                        property_iri=str(prop_uri), property_label=prop_label, values=values
                    )
                )

        return OWLClassResponse(
            iri=str(class_uri),  # type: ignore[arg-type]  # Pydantic coerces str to HttpUrl
            labels=labels,
            comments=comments,
            deprecated=deprecated,
            parent_iris=parent_iris,
            parent_labels=parent_labels,
            equivalent_iris=[],
            disjoint_iris=[],
            child_count=child_count,
            instance_count=instance_count,
            annotations=annotations,
        )


# Singleton instance for caching (shares graph cache across requests)
_ontology_service: OntologyService | None = None


def get_ontology_service(storage: StorageService | None = None) -> OntologyService:
    """
    Get the ontology service singleton.

    The singleton pattern is used to share the graph cache across requests.
    """
    global _ontology_service
    if _ontology_service is None:
        _ontology_service = OntologyService(storage=storage)
    elif storage is not None and _ontology_service._storage is None:
        _ontology_service._storage = storage
    return _ontology_service
