"""Ontology metadata extraction service using RDFLib."""

from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, URIRef
from rdflib.namespace import DC, DCTERMS, OWL, RDF, RDFS


@dataclass
class ExtractedMetadata:
    """Metadata extracted from an ontology file."""

    ontology_iri: str | None
    title: str | None
    description: str | None
    format_detected: str


class OntologyParseError(Exception):
    """Exception raised when ontology parsing fails."""

    pass


class UnsupportedFormatError(Exception):
    """Exception raised when file format is not supported."""

    pass


class OntologyMetadataExtractor:
    """Service for extracting metadata from ontology files."""

    # Map file extensions to RDFLib format strings
    FORMAT_MAP: dict[str, str] = {
        ".owl": "xml",
        ".rdf": "xml",
        ".ttl": "turtle",
        ".n3": "n3",
        ".jsonld": "json-ld",
    }

    # Content type mapping for storage
    CONTENT_TYPE_MAP: dict[str, str] = {
        ".owl": "application/rdf+xml",
        ".rdf": "application/rdf+xml",
        ".ttl": "text/turtle",
        ".n3": "text/n3",
        ".jsonld": "application/ld+json",
    }

    # Supported extensions for validation
    SUPPORTED_EXTENSIONS = frozenset(FORMAT_MAP.keys())

    @classmethod
    def get_format_for_extension(cls, extension: str) -> str | None:
        """Get the RDFLib format string for a file extension."""
        return cls.FORMAT_MAP.get(extension.lower())

    @classmethod
    def get_content_type(cls, extension: str) -> str:
        """Get the content type for a file extension."""
        return cls.CONTENT_TYPE_MAP.get(extension.lower(), "application/octet-stream")

    @classmethod
    def is_supported_extension(cls, extension: str) -> bool:
        """Check if the file extension is supported."""
        return extension.lower() in cls.SUPPORTED_EXTENSIONS

    def extract_metadata(self, content: bytes, filename: str) -> ExtractedMetadata:
        """
        Extract metadata from an ontology file.

        Args:
            content: The file content as bytes
            filename: The original filename (used to determine format)

        Returns:
            ExtractedMetadata with ontology IRI, title, description, and format

        Raises:
            UnsupportedFormatError: If the file format is not supported
            OntologyParseError: If the file cannot be parsed
        """
        extension = Path(filename).suffix.lower()

        if not self.is_supported_extension(extension):
            supported = ", ".join(sorted(self.SUPPORTED_EXTENSIONS))
            raise UnsupportedFormatError(
                f"Unsupported file format: {extension}. Supported formats: {supported}"
            )

        rdf_format = self.get_format_for_extension(extension)
        if rdf_format is None:
            raise UnsupportedFormatError(f"Could not determine format for: {extension}")

        try:
            graph = Graph()
            graph.parse(data=content, format=rdf_format)
        except Exception as e:
            raise OntologyParseError(f"Failed to parse ontology: {e}") from e

        # Find the ontology IRI
        ontology_iri = self._find_ontology_iri(graph)

        # Extract title and description
        title = self._extract_title(graph, ontology_iri)
        description = self._extract_description(graph, ontology_iri)

        return ExtractedMetadata(
            ontology_iri=ontology_iri,
            title=title,
            description=description,
            format_detected=rdf_format,
        )

    def _find_ontology_iri(self, graph: Graph) -> str | None:
        """Find the ontology IRI (subject of rdf:type owl:Ontology)."""
        for subject in graph.subjects(RDF.type, OWL.Ontology):
            if isinstance(subject, URIRef):
                return str(subject)
        return None

    def _extract_title(self, graph: Graph, ontology_iri: str | None) -> str | None:
        """
        Extract the title from the ontology.

        Priority: dc:title > dcterms:title > rdfs:label
        """
        # Define subjects to check (ontology IRI first, then any subject)
        subjects_to_check: list[URIRef | None] = []
        if ontology_iri:
            subjects_to_check.append(URIRef(ontology_iri))

        # Priority order for title properties
        title_properties = [DC.title, DCTERMS.title, RDFS.label]

        for subject in subjects_to_check:
            for prop in title_properties:
                for obj in graph.objects(subject, prop):
                    value = str(obj)
                    if value:
                        return value

        # If no ontology IRI, search globally for any title on owl:Ontology subjects
        for subject in graph.subjects(RDF.type, OWL.Ontology):
            for prop in title_properties:
                for obj in graph.objects(subject, prop):
                    value = str(obj)
                    if value:
                        return value

        return None

    def _extract_description(self, graph: Graph, ontology_iri: str | None) -> str | None:
        """
        Extract the description from the ontology.

        Priority: dc:description > dcterms:description > rdfs:comment
        """
        # Define subjects to check (ontology IRI first)
        subjects_to_check: list[URIRef | None] = []
        if ontology_iri:
            subjects_to_check.append(URIRef(ontology_iri))

        # Priority order for description properties
        desc_properties = [DC.description, DCTERMS.description, RDFS.comment]

        for subject in subjects_to_check:
            for prop in desc_properties:
                for obj in graph.objects(subject, prop):
                    value = str(obj)
                    if value:
                        return value

        # If no ontology IRI, search globally for any description on owl:Ontology subjects
        for subject in graph.subjects(RDF.type, OWL.Ontology):
            for prop in desc_properties:
                for obj in graph.objects(subject, prop):
                    value = str(obj)
                    if value:
                        return value

        return None


def get_ontology_extractor() -> OntologyMetadataExtractor:
    """Factory function for dependency injection."""
    return OntologyMetadataExtractor()
