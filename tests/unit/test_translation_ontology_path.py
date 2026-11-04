"""Translation coverage reads the project's configured ontology file (hosted UAT).

GitHub-imported demo repos hold several ontology-like files. Coverage used to
take ``ontology.ttl`` or else the first ``.ttl/.owl/.rdf`` in the tree and
parse it as Turtle, so FOLIO's RDF/XML ``FOLIO.owl`` made
``GET /translation/entity-state`` return 500.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from rdflib import RDFS, Graph, Literal, URIRef

from ontokit.services.ontology import rdf_format_for_filename
from ontokit.services.translation_coverage import TranslationCoverageService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
ENTITY = URIRef("http://example.org/ont#Thing")

_GRAPH = Graph()
_GRAPH.add((ENTITY, RDFS.label, Literal("Thing", lang="en")))
TURTLE = _GRAPH.serialize(format="turtle").encode()
RDF_XML = _GRAPH.serialize(format="xml").encode()


def _project(
    *, turtle_path: str | None = None, ontology_path: str | None = None, source: str | None = None
) -> SimpleNamespace:
    integration = (
        SimpleNamespace(turtle_file_path=turtle_path, ontology_file_path=ontology_path)
        if turtle_path or ontology_path
        else None
    )
    return SimpleNamespace(github_integration=integration, source_file_path=source)


def _service(project: object, files: dict[str, bytes]) -> tuple[TranslationCoverageService, Any]:
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=project)
    git = MagicMock()
    git.get_repository.return_value.list_files.return_value = list(files)

    def read(_project_id: uuid.UUID, _branch: str, filename: str) -> bytes:
        return files[filename]

    git.get_file_from_branch.side_effect = read
    return TranslationCoverageService(db, git), git


def _read_paths(git: MagicMock) -> list[str]:
    return [call.args[2] for call in git.get_file_from_branch.call_args_list]


async def test_folio_layout_reads_configured_turtle_not_rdf_xml_sibling() -> None:
    files = {
        "FOLIO.owl": RDF_XML,
        "FOLIO-webprotege-merge-output.owl": RDF_XML,
        "FOLIO.ttl": TURTLE,
        "README.md": b"# FOLIO",
    }
    service, git = _service(_project(turtle_path="FOLIO.ttl"), files)

    graph = await service._load_branch_graph(PROJECT_ID, "main")

    assert (ENTITY, RDFS.label, Literal("Thing", lang="en")) in graph
    assert _read_paths(git) == ["FOLIO.ttl"]


async def test_canon_layout_reads_nested_configured_turtle() -> None:
    files = {
        "sources/ontology-semantic-canon.owl": RDF_XML,
        "sources/ontology-semantic-canon.owx": b"<Ontology/>",
        "sources/ontology-semantic-canon.ttl": TURTLE,
    }
    project = _project(
        turtle_path="sources/ontology-semantic-canon.ttl",
        ontology_path="sources/ontology-semantic-canon.owl",
    )
    service, git = _service(project, files)

    await service._load_branch_graph(PROJECT_ID, "main")

    assert _read_paths(git) == ["sources/ontology-semantic-canon.ttl"]


async def test_legacy_ontology_ttl_project_is_unchanged() -> None:
    files = {"ontology.ttl": TURTLE}
    service, git = _service(_project(source="ontokit/projects/x/ontology.ttl"), files)

    graph = await service._load_branch_graph(PROJECT_ID, "main")

    assert len(graph) == 1
    assert _read_paths(git) == ["ontology.ttl"]


async def test_configured_rdf_xml_is_parsed_as_rdf_xml() -> None:
    files = {"FOLIO.owl": RDF_XML}
    service, _git = _service(_project(ontology_path="FOLIO.owl"), files)

    graph = await service._load_branch_graph(PROJECT_ID, "main")

    assert (ENTITY, RDFS.label, Literal("Thing", lang="en")) in graph


async def test_unconfigured_fallback_prefers_turtle_over_rdf_xml() -> None:
    # Configured path absent from the tree: fall back, preferring .ttl.
    files = {"a.owl": RDF_XML, "z.ttl": TURTLE}
    service, git = _service(_project(), files)

    await service._load_branch_graph(PROJECT_ID, "main")

    assert _read_paths(git) == ["z.ttl"]


async def test_unconfigured_fallback_skips_owl_xml_serializations() -> None:
    # .owx is OWL/XML, not RDF/XML; the heuristic never guesses it.
    files = {"a.owx": b"<Ontology/>", "b.owl": RDF_XML}
    service, git = _service(_project(), files)

    await service._load_branch_graph(PROJECT_ID, "main")

    assert _read_paths(git) == ["b.owl"]


async def test_unconfigured_fallback_parses_rdf_xml_by_extension() -> None:
    files = {"only.owl": RDF_XML}
    service, _git = _service(_project(), files)

    graph = await service._load_branch_graph(PROJECT_ID, "main")

    assert len(graph) == 1


async def test_no_ontology_file_is_a_clear_error() -> None:
    service, _git = _service(_project(), {"README.md": b"x"})

    with pytest.raises(FileNotFoundError, match="No ontology file"):
        await service._load_branch_graph(PROJECT_ID, "main")


async def test_entity_state_on_folio_layout_succeeds() -> None:
    files = {"FOLIO.owl": RDF_XML, "FOLIO.ttl": TURTLE}
    service, git = _service(_project(turtle_path="FOLIO.ttl"), files)
    empty = MagicMock()
    empty.all.return_value = []
    empty.scalars.return_value.all.return_value = []
    empty.scalar_one_or_none.return_value = None
    service._db.execute = AsyncMock(return_value=empty)  # type: ignore[union-attr]

    state = await service.entity_state(PROJECT_ID, str(ENTITY), "main")

    assert state["entity_iri"] == str(ENTITY)
    assert _read_paths(git) == ["FOLIO.ttl"]


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("FOLIO.ttl", "turtle"),
        ("FOLIO.owl", "xml"),
        ("x.RDF", "xml"),
        ("x.jsonld", "json-ld"),
    ],
)
def test_rdf_format_for_filename(filename: str, expected: str) -> None:
    assert rdf_format_for_filename(filename) == expected


def test_rdf_format_for_unknown_extension_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        rdf_format_for_filename("ontology.foo")
