"""Duplicate detection service — find entities with similar labels.

Uses PostgreSQL's pg_trgm extension with the existing GIN trigram index on
indexed_labels.value for fast fuzzy matching.  Falls back to the legacy
in-memory rdflib approach when the project has no PostgreSQL index.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.schemas.quality import DuplicateCluster, DuplicateDetectionResult, DuplicateEntity


class DisjointSet:
    """Union-find data structure with path compression."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        while self._parent.get(x, x) != x:
            self._parent[x] = self._parent.get(self._parent[x], self._parent[x])
            x = self._parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        px, py = self.find(x), self.find(y)
        if px != py:
            self._parent[px] = py


async def find_duplicates_sql(
    db: AsyncSession,
    project_id: UUID,
    branch: str,
    threshold: float = 0.85,
) -> DuplicateDetectionResult:
    """Find duplicate entities using PostgreSQL trigram similarity.

    For each rdfs:label in the project, queries the GIN trigram index for
    similar labels belonging to *other* entities of the same type.  This
    avoids parsing the ontology file and runs in seconds rather than minutes.
    """
    # Set the similarity threshold for this transaction
    await db.execute(text(f"SET LOCAL pg_trgm.similarity_threshold = {float(threshold)}"))

    # Step 1: Get one rdfs:label per entity for this project/branch
    labels_result = await db.execute(
        text("""
            SELECT DISTINCT ON (e.id)
                e.id AS entity_id, e.iri, e.entity_type, l.value
            FROM indexed_entities e
            JOIN indexed_labels l ON l.entity_id = e.id
            WHERE e.project_id = :project_id
              AND e.branch = :branch
              AND l.property_iri = 'http://www.w3.org/2000/01/rdf-schema#label'
            ORDER BY e.id, l.lang NULLS LAST
        """),
        {"project_id": project_id, "branch": branch},
    )
    entities = labels_result.fetchall()

    # Build lookup maps
    entity_ids = {str(row.entity_id) for row in entities}
    entity_map: dict[str, tuple[str, str, str]] = {}  # entity_id → (iri, label, type)
    for row in entities:
        entity_map[str(row.entity_id)] = (row.iri, row.value, row.entity_type)

    # Step 2: For each entity's label, find similar labels using GIN index.
    # The GIN trigram index on indexed_labels.value makes `value % 'literal'`
    # a sub-linear indexed lookup instead of a full table scan.
    pair_sim: dict[tuple[str, str], float] = {}

    for row in entities:
        similar = await db.execute(
            text("""
                SELECT e.id AS entity_id, e.iri, l.value,
                       similarity(l.value, :label) AS sim
                FROM indexed_labels l
                JOIN indexed_entities e ON l.entity_id = e.id
                WHERE l.value % :label
                  AND l.property_iri = 'http://www.w3.org/2000/01/rdf-schema#label'
                  AND e.project_id = :project_id
                  AND e.branch = :branch
                  AND e.entity_type = :entity_type
                  AND e.id != :entity_id
                ORDER BY sim DESC
                LIMIT 50
            """),
            {
                "label": row.value,
                "project_id": project_id,
                "branch": branch,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
            },
        )
        for match in similar:
            mid = str(match.entity_id)
            if mid not in entity_ids:
                continue
            key = (row.iri, match.iri)
            sim = float(match.sim)
            pair_sim[key] = max(pair_sim.get(key, 0.0), sim)

    # Build clusters via union-find from the collected pair_sim data
    ds = DisjointSet()

    # Union all matched pairs and build entity_info lookup
    entity_info: dict[str, tuple[str, str]] = {}  # iri → (label, entity_type)
    iri_lookup = {iri: (label, etype) for _eid, (iri, label, etype) in entity_map.items()}

    for iri_a, iri_b in pair_sim:
        ds.union(iri_a, iri_b)
        if iri_a in iri_lookup:
            entity_info[iri_a] = (iri_lookup[iri_a][0], iri_lookup[iri_a][1])
        if iri_b in iri_lookup:
            entity_info[iri_b] = (iri_lookup[iri_b][0], iri_lookup[iri_b][1])

    # Build clusters
    clusters_map: dict[str, list[str]] = defaultdict(list)
    for iri in entity_info:
        clusters_map[ds.find(iri)].append(iri)

    clusters = []
    for _root, members in clusters_map.items():
        if len(members) < 2:
            continue
        sims = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                key = (members[i], members[j])
                rev = (members[j], members[i])
                if key in pair_sim:
                    sims.append(pair_sim[key])
                elif rev in pair_sim:
                    sims.append(pair_sim[rev])
        avg_sim = sum(sims) / len(sims) if sims else threshold

        clusters.append(
            DuplicateCluster(
                entities=[
                    DuplicateEntity(
                        iri=iri,
                        label=entity_info[iri][0],
                        entity_type=entity_info[iri][1],
                    )
                    for iri in members
                ],
                similarity=round(avg_sim, 3),
            )
        )

    return DuplicateDetectionResult(
        clusters=clusters,
        threshold=threshold,
        checked_at=datetime.now(UTC).isoformat(),
    )


# Legacy in-memory approach (kept for projects without a PostgreSQL index)


def find_duplicates(graph: Graph, threshold: float = 0.85) -> DuplicateDetectionResult:  # type: ignore[name-defined]  # noqa: F821
    """Find entities with similar labels using string similarity (legacy, slow)."""
    import difflib

    from rdflib import Literal as RDFLiteral
    from rdflib import URIRef  # noqa: F811
    from rdflib.namespace import OWL, RDF, RDFS

    from ontokit.services.rdf_utils import get_entity_type as _get_entity_type

    entities: list[tuple[str, str, str]] = []
    for s in graph.subjects(RDF.type, None):
        if not isinstance(s, URIRef) or s == OWL.Thing:
            continue
        etype = _get_entity_type(graph, s)
        if etype == "unknown":
            continue
        for obj in graph.objects(s, RDFS.label):
            if isinstance(obj, RDFLiteral):
                entities.append((str(s), str(obj), etype))
                break

    by_type: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for iri, label, etype in entities:
        by_type[etype].append((iri, label))

    ds = DisjointSet()
    pair_sim: dict[tuple[str, str], float] = {}

    for _etype, etype_entities in by_type.items():
        n = len(etype_entities)
        for i in range(n):
            iri_a, label_a = etype_entities[i]
            norm_a = label_a.lower().strip()
            for j in range(i + 1, n):
                iri_b, label_b = etype_entities[j]
                norm_b = label_b.lower().strip()
                sim = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
                pair_sim[(iri_a, iri_b)] = sim
                if sim >= threshold:
                    ds.union(iri_a, iri_b)

    clusters_map: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    entity_lookup = {iri: (label, etype) for iri, label, etype in entities}
    for iri, (label, etype) in entity_lookup.items():
        root = ds.find(iri)
        clusters_map[root].append((iri, label, etype))

    clusters = []
    for _root, members in clusters_map.items():
        if len(members) < 2:
            continue
        sims = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                key = (members[i][0], members[j][0])
                rev_key = (members[j][0], members[i][0])
                if key in pair_sim:
                    sim = pair_sim[key]
                elif rev_key in pair_sim:
                    sim = pair_sim[rev_key]
                else:
                    continue
                if sim is not None:
                    sims.append(sim)
        avg_sim = sum(sims) / len(sims) if sims else threshold

        clusters.append(
            DuplicateCluster(
                entities=[
                    DuplicateEntity(iri=iri, label=label, entity_type=etype)
                    for iri, label, etype in members
                ],
                similarity=round(avg_sim, 3),
            )
        )

    return DuplicateDetectionResult(
        clusters=clusters,
        threshold=threshold,
        checked_at=datetime.now(UTC).isoformat(),
    )
