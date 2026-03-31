"""Project and ontology endpoints - adapts FOLIO data to OntoKit web format."""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter()

# A fixed project ID representing the FOLIO ontology
FOLIO_PROJECT_ID = "00000000-0000-0000-0000-000000000001"

FOLIO_BASE = "https://folio.openlegalstandard.org/"

# Root classes are computed dynamically at startup
_root_classes_cache: list | None = None


def _get_root_classes(folio) -> list:
    """Find root classes dynamically: no parents (or only owl:Thing), with children."""
    global _root_classes_cache
    if _root_classes_cache is not None:
        return _root_classes_cache
    roots = []
    for cls in folio.classes:
        parents = cls.sub_class_of or []
        if not parents or all(p.endswith("Thing") for p in parents):
            children = cls.parent_class_of or []
            if len(children) > 0:
                roots.append(cls)
    _root_classes_cache = roots
    return roots


# --- Pydantic response models matching ontokit-web expectations ---


class ProjectOwner(BaseModel):
    id: str
    name: str | None = None
    email: str | None = None


class Project(BaseModel):
    id: str
    name: str
    description: str | None = None
    is_public: bool = True
    owner_id: str
    owner: ProjectOwner | None = None
    created_at: str
    updated_at: str | None = None
    member_count: int = 1
    user_role: str | None = "owner"
    is_superadmin: bool = True
    source_file_path: str | None = None
    git_ontology_path: str | None = None
    ontology_iri: str | None = None
    label_preferences: list[str] | None = None


class ProjectListResponse(BaseModel):
    items: list[Project]
    total: int
    skip: int
    limit: int


class OWLClassTreeNode(BaseModel):
    iri: str
    label: str
    child_count: int
    deprecated: bool = False


class OWLClassTreeResponse(BaseModel):
    nodes: list[OWLClassTreeNode]
    total_classes: int


class LocalizedString(BaseModel):
    value: str
    lang: str = "en"


class AnnotationProperty(BaseModel):
    property_iri: str
    property_label: str
    values: list[LocalizedString]


class OWLClassResponse(BaseModel):
    iri: str
    labels: list[LocalizedString]
    comments: list[LocalizedString]
    deprecated: bool = False
    parent_iris: list[str]
    parent_labels: dict[str, str]
    equivalent_iris: list[str] = []
    disjoint_iris: list[str] = []
    child_count: int = 0
    instance_count: int = 0
    is_defined: bool = True
    source_ontology: str | None = None
    annotations: list[AnnotationProperty] = []


class EntitySearchResult(BaseModel):
    iri: str
    label: str
    entity_type: str
    deprecated: bool = False


class EntitySearchResponse(BaseModel):
    results: list[EntitySearchResult]
    total: int


# --- Helper functions ---


def _get_folio(request: Request):
    return request.app.state.folio


def _make_folio_project() -> Project:
    return Project(
        id=FOLIO_PROJECT_ID,
        name="FOLIO - Federated Open Legal Information Ontology",
        description=(
            "An open standard with 18,000+ concepts for representing "
            "universal elements of legal data."
        ),
        is_public=True,
        owner_id="folio-system",
        owner=ProjectOwner(id="folio-system", name="ALEA Institute"),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        member_count=1,
        user_role="owner",
        is_superadmin=True,
        ontology_iri=FOLIO_BASE,
        source_file_path="FOLIO.owl",
        git_ontology_path="FOLIO.owl",
        label_preferences=["rdfs:label", "skos:prefLabel"],
    )


def _class_to_tree_node(owl_class) -> OWLClassTreeNode:
    """Convert a folio-python OWLClass to an OntoKit tree node."""
    child_iris = owl_class.parent_class_of or []
    label = owl_class.label or owl_class.preferred_label or _local_name(owl_class.iri)
    return OWLClassTreeNode(
        iri=owl_class.iri,
        label=label,
        child_count=len(child_iris),
        deprecated=bool(owl_class.deprecated),
    )


def _class_to_detail(owl_class, folio) -> OWLClassResponse:
    """Convert a folio-python OWLClass to an OntoKit class detail response."""
    label = owl_class.label or owl_class.preferred_label or _local_name(owl_class.iri)
    parent_iris = owl_class.sub_class_of or []
    child_count = len(owl_class.parent_class_of or [])

    # Resolve parent labels
    parent_labels = {}
    for p_iri in parent_iris:
        p_class = folio[p_iri]
        if p_class:
            p_label = p_class.label or p_class.preferred_label or _local_name(p_iri)
            parent_labels[p_iri] = p_label
        else:
            parent_labels[p_iri] = _local_name(p_iri)

    # Build labels list
    labels = [LocalizedString(value=label, lang="en")]
    if owl_class.preferred_label and owl_class.preferred_label != label:
        labels.append(LocalizedString(value=owl_class.preferred_label, lang="en"))

    # Build comments
    comments = []
    if owl_class.comment:
        comments.append(LocalizedString(value=owl_class.comment, lang="en"))

    # Build annotations from SKOS/DC metadata
    annotations = []
    if owl_class.definition:
        annotations.append(AnnotationProperty(
            property_iri="http://www.w3.org/2004/02/skos/core#definition",
            property_label="skos:definition",
            values=[LocalizedString(value=owl_class.definition)],
        ))
    if owl_class.alternative_labels:
        for alt in owl_class.alternative_labels:
            annotations.append(AnnotationProperty(
                property_iri="http://www.w3.org/2004/02/skos/core#altLabel",
                property_label="skos:altLabel",
                values=[LocalizedString(value=alt)],
            ))
    if owl_class.examples:
        for ex in owl_class.examples:
            annotations.append(AnnotationProperty(
                property_iri="http://www.w3.org/2004/02/skos/core#example",
                property_label="skos:example",
                values=[LocalizedString(value=ex)],
            ))
    if owl_class.notes:
        for note in owl_class.notes:
            annotations.append(AnnotationProperty(
                property_iri="http://www.w3.org/2004/02/skos/core#note",
                property_label="skos:note",
                values=[LocalizedString(value=note)],
            ))
    if owl_class.is_defined_by:
        annotations.append(AnnotationProperty(
            property_iri="http://www.w3.org/2000/01/rdf-schema#isDefinedBy",
            property_label="rdfs:isDefinedBy",
            values=[LocalizedString(value=owl_class.is_defined_by)],
        ))
    if owl_class.identifier:
        annotations.append(AnnotationProperty(
            property_iri="http://purl.org/dc/elements/1.1/identifier",
            property_label="dc:identifier",
            values=[LocalizedString(value=owl_class.identifier)],
        ))
    if owl_class.description:
        annotations.append(AnnotationProperty(
            property_iri="http://purl.org/dc/elements/1.1/description",
            property_label="dc:description",
            values=[LocalizedString(value=owl_class.description)],
        ))
    if owl_class.see_also:
        for sa in owl_class.see_also:
            annotations.append(AnnotationProperty(
                property_iri="http://www.w3.org/2000/01/rdf-schema#seeAlso",
                property_label="rdfs:seeAlso",
                values=[LocalizedString(value=sa)],
            ))

    # Translations
    if owl_class.translations:
        for lang_code, translated in owl_class.translations.items():
            if translated:
                annotations.append(AnnotationProperty(
                    property_iri="http://www.w3.org/2004/02/skos/core#prefLabel",
                    property_label=f"skos:prefLabel@{lang_code}",
                    values=[LocalizedString(value=translated, lang=lang_code)],
                ))

    return OWLClassResponse(
        iri=owl_class.iri,
        labels=labels,
        comments=comments,
        deprecated=bool(owl_class.deprecated),
        parent_iris=parent_iris,
        parent_labels=parent_labels,
        child_count=child_count,
        instance_count=0,
        is_defined=True,
        source_ontology=FOLIO_BASE,
        annotations=annotations,
    )


def _local_name(iri: str) -> str:
    """Extract local name from an IRI."""
    if "#" in iri:
        return iri.rsplit("#", 1)[-1]
    return iri.rsplit("/", 1)[-1]


def _decode_iri(iri_path: str) -> str:
    """Decode a URL-encoded IRI from a path parameter."""
    return urllib.parse.unquote(iri_path)


def _resolve_class(folio, iri: str):
    """Try to resolve a class by full IRI or suffix."""
    # Try direct lookup
    owl_class = folio[iri]
    if owl_class:
        return owl_class
    # Try as suffix (without base)
    if not iri.startswith("http"):
        owl_class = folio[FOLIO_BASE + iri]
        if owl_class:
            return owl_class
    return None


# --- Project endpoints ---


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(
    request: Request,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    filter: str | None = None,
):
    return ProjectListResponse(
        items=[_make_folio_project()],
        total=1,
        skip=skip,
        limit=limit,
    )


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(request: Request, project_id: str):
    if project_id != FOLIO_PROJECT_ID:
        raise HTTPException(status_code=404, detail="Project not found")
    return _make_folio_project()


# --- Ontology tree endpoints ---


@router.get(
    "/projects/{project_id}/ontology/tree",
    response_model=OWLClassTreeResponse,
)
async def get_tree_root(request: Request, project_id: str, branch: str | None = None):
    folio = _get_folio(request)
    roots = _get_root_classes(folio)
    nodes = [_class_to_tree_node(cls) for cls in roots]
    nodes.sort(key=lambda n: n.label.lower())
    return OWLClassTreeResponse(nodes=nodes, total_classes=len(folio.classes))


@router.get(
    "/projects/{project_id}/ontology/tree/{class_iri:path}/children",
    response_model=OWLClassTreeResponse,
)
async def get_tree_children(
    request: Request, project_id: str, class_iri: str, branch: str | None = None
):
    folio = _get_folio(request)
    iri = _decode_iri(class_iri)
    owl_class = _resolve_class(folio, iri)
    if not owl_class:
        raise HTTPException(status_code=404, detail=f"Class not found: {iri}")

    nodes = []
    for child_iri in (owl_class.parent_class_of or []):
        child = folio[child_iri]
        if child:
            nodes.append(_class_to_tree_node(child))
    nodes.sort(key=lambda n: n.label.lower())
    return OWLClassTreeResponse(nodes=nodes, total_classes=len(folio.classes))


@router.get(
    "/projects/{project_id}/ontology/tree/{class_iri:path}/ancestors",
    response_model=OWLClassTreeResponse,
)
async def get_tree_ancestors(
    request: Request, project_id: str, class_iri: str, branch: str | None = None
):
    """Return ancestor path from root to the given class (not including the class itself)."""
    folio = _get_folio(request)
    iri = _decode_iri(class_iri)
    owl_class = _resolve_class(folio, iri)
    if not owl_class:
        raise HTTPException(status_code=404, detail=f"Class not found: {iri}")

    # Walk up parent chain
    ancestors = []
    visited = {iri}
    current = owl_class
    while current.sub_class_of:
        parent_iri = current.sub_class_of[0]
        if parent_iri in visited:
            break
        visited.add(parent_iri)
        parent = folio[parent_iri]
        if not parent:
            break
        ancestors.append(_class_to_tree_node(parent))
        current = parent

    # Reverse so it goes root -> ... -> parent
    ancestors.reverse()
    return OWLClassTreeResponse(nodes=ancestors, total_classes=len(folio.classes))


# --- Class detail endpoint ---


@router.get(
    "/projects/{project_id}/ontology/classes/{class_iri:path}",
    response_model=OWLClassResponse,
)
async def get_class_detail(
    request: Request, project_id: str, class_iri: str, branch: str | None = None
):
    folio = _get_folio(request)
    iri = _decode_iri(class_iri)
    owl_class = _resolve_class(folio, iri)
    if not owl_class:
        raise HTTPException(status_code=404, detail=f"Class not found: {iri}")
    return _class_to_detail(owl_class, folio)


# --- Search endpoint ---


@router.get(
    "/projects/{project_id}/ontology/search",
    response_model=EntitySearchResponse,
)
async def search_entities(
    request: Request,
    project_id: str,
    q: str = Query(min_length=1, max_length=200),
    entity_types: str | None = None,
    branch: str | None = None,
):
    folio = _get_folio(request)
    query_lower = q.lower()
    results: list[EntitySearchResult] = []
    limit = 50

    # Search classes
    include_classes = not entity_types or "class" in entity_types
    include_properties = not entity_types or "property" in entity_types

    if include_classes:
        # Use prefix search first for best matches
        prefix_matches = folio.search_by_prefix(q)
        seen_iris = set()
        for cls in prefix_matches[:limit]:
            label = cls.label or cls.preferred_label or _local_name(cls.iri)
            results.append(EntitySearchResult(
                iri=cls.iri,
                label=label,
                entity_type="class",
                deprecated=bool(cls.deprecated),
            ))
            seen_iris.add(cls.iri)

        # Supplement with substring search if needed
        if len(results) < limit:
            for cls in folio.classes:
                if cls.iri in seen_iris:
                    continue
                label = cls.label or cls.preferred_label or ""
                alt_labels = cls.alternative_labels or []
                definition = cls.definition or ""
                searchable = (
                    label.lower()
                    + " ".join(a.lower() for a in alt_labels)
                    + definition.lower()
                )
                if query_lower in searchable:
                    results.append(EntitySearchResult(
                        iri=cls.iri,
                        label=label or _local_name(cls.iri),
                        entity_type="class",
                        deprecated=bool(cls.deprecated),
                    ))
                    seen_iris.add(cls.iri)
                    if len(results) >= limit:
                        break

    if include_properties and len(results) < limit:
        for prop in folio.object_properties:
            label = prop.label or prop.preferred_label or ""
            if query_lower in label.lower() or query_lower in prop.iri.lower():
                results.append(EntitySearchResult(
                    iri=prop.iri,
                    label=label or _local_name(prop.iri),
                    entity_type="property",
                    deprecated=False,
                ))
                if len(results) >= limit:
                    break

    return EntitySearchResponse(results=results[:limit], total=len(results))


# --- Stub endpoints for features the web UI may call ---


@router.get("/projects/{project_id}/branches")
async def list_branches(request: Request, project_id: str):
    return {
        "items": [
            {
                "name": "main",
                "is_current": True,
                "is_default": True,
                "commit_hash": "folio-main",
                "commit_message": "FOLIO ontology",
                "commit_date": datetime.now(timezone.utc).isoformat(),
                "commits_ahead": 0,
                "commits_behind": 0,
                "remote_commits_ahead": None,
                "remote_commits_behind": None,
                "can_delete": False,
                "has_open_pr": False,
                "has_delete_permission": False,
            }
        ],
        "current_branch": "main",
        "default_branch": "main",
        "preferred_branch": None,
        "has_github_remote": False,
        "last_sync_at": None,
        "sync_status": None,
    }


@router.get("/projects/{project_id}/branches/preference")
async def get_branch_preference(request: Request, project_id: str):
    return {"branch": "main"}


@router.put("/projects/{project_id}/branches/preference")
async def save_branch_preference(request: Request, project_id: str):
    return {"branch": "main"}


@router.get("/projects/{project_id}/source")
async def get_source(request: Request, project_id: str, branch: str | None = None):
    return {"content": "", "format": "turtle"}


@router.get("/projects/{project_id}/revisions")
async def list_revisions(request: Request, project_id: str, branch: str | None = None):
    return {"items": [], "total": 0}


@router.get("/projects/{project_id}/revisions/file")
async def get_file_at_version(
    request: Request,
    project_id: str,
    version: str | None = None,
    filename: str | None = None,
):
    return {
        "project_id": project_id,
        "version": version or "main",
        "filename": filename or "FOLIO.owl",
        "content": "",
    }


@router.get("/projects/{project_id}/pull-requests")
async def list_pull_requests(
    request: Request,
    project_id: str,
    status: str | None = None,
    skip: int = 0,
    limit: int = 20,
):
    return {"items": [], "total": 0, "skip": skip, "limit": limit}


@router.get("/projects/{project_id}/suggestions")
async def list_suggestions(request: Request, project_id: str, status: str | None = None):
    return {"items": [], "total": 0}


@router.get("/projects/{project_id}/lint")
async def get_lint_status(request: Request, project_id: str, branch: str | None = None):
    return {"status": "clean", "issues": [], "total": 0}


@router.get("/projects/{project_id}/normalization")
async def get_normalization(request: Request, project_id: str):
    return {"status": "normalized"}


@router.get("/projects/{project_id}/analytics")
async def get_analytics(request: Request, project_id: str):
    folio = _get_folio(request)
    return {
        "total_classes": len(folio.classes),
        "total_properties": len(folio.object_properties),
        "total_individuals": 0,
    }


@router.get("/projects/{project_id}/embeddings")
async def list_embeddings(request: Request, project_id: str):
    return {"items": [], "total": 0}
