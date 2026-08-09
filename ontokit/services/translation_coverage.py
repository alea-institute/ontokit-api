"""Read-only translation coverage and provenance projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from rdflib import Graph, Literal, URIRef
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.git import GitRepositoryService
from ontokit.models.ontology_index import IndexedEntity, IndexedLabel
from ontokit.models.translation import (
    ProjectTranslationConfig,
    TranslationJob,
    TranslationRecord,
    hash_literal_value,
)
from ontokit.services.translation_annotations import read_annotation, translation_record_digest


@dataclass(frozen=True, slots=True)
class LabelValue:
    entity_iri: str
    predicate: str
    language: str | None
    value: str
    value_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_hash", hash_literal_value(self.value))


class TranslationCoverageService:
    """Build branch-local views without invoking any translation provider."""

    def __init__(self, db: AsyncSession | None, git_service: GitRepositoryService | None) -> None:
        self._db = db
        self._git = git_service

    async def coverage(self, project_id: UUID, branch: str) -> dict[str, Any]:
        languages, labels, records, graph = await self._load(project_id, branch)
        candidates = {
            (entity, predicate, language)
            for entity, predicate in self._source_slots(labels, records)
            for language in languages
        }
        pending = await self._pending_scopes(project_id, branch, candidates)
        return self.classify(branch, languages, labels, records, graph, pending)

    async def entity_state(self, project_id: UUID, entity_iri: str, branch: str) -> dict[str, Any]:
        languages, labels, records, graph = await self._load(project_id, branch)
        candidates = {
            (entity, predicate, language)
            for entity, predicate in self._source_slots(labels, records)
            if entity == entity_iri
            for language in languages
        }
        pending = await self._pending_scopes(project_id, branch, candidates)
        return self.entity_state_from_data(
            entity_iri, branch, languages, labels, records, graph, pending
        )

    async def provisional(
        self, project_id: UUID, language: str, branch: str
    ) -> list[dict[str, Any]]:
        labels, records = await self._load_provisional(project_id, branch)
        return self.provisional_from_data(language, labels, records)

    async def _load_provisional(
        self, project_id: UUID, branch: str
    ) -> tuple[list[LabelValue], list[TranslationRecord]]:
        if self._db is None:
            raise RuntimeError("coverage service I/O dependencies are not configured")
        label_result = await self._db.execute(
            select(
                IndexedEntity.iri, IndexedLabel.property_iri, IndexedLabel.lang, IndexedLabel.value
            )
            .join(IndexedLabel, IndexedLabel.entity_id == IndexedEntity.id)
            .where(IndexedEntity.project_id == project_id, IndexedEntity.branch == branch)
        )
        labels = [LabelValue(*row) for row in label_result.all()]
        record_result = await self._db.execute(
            select(TranslationRecord).where(TranslationRecord.project_id == project_id)
        )
        return labels, list(record_result.scalars().all())

    async def _load(
        self, project_id: UUID, branch: str
    ) -> tuple[list[str], list[LabelValue], list[TranslationRecord], Graph]:
        if self._db is None or self._git is None:
            raise RuntimeError("coverage service I/O dependencies are not configured")
        config_result = await self._db.execute(
            select(ProjectTranslationConfig).where(
                ProjectTranslationConfig.project_id == project_id
            )
        )
        config = config_result.scalar_one_or_none()
        label_result = await self._db.execute(
            select(
                IndexedEntity.iri, IndexedLabel.property_iri, IndexedLabel.lang, IndexedLabel.value
            )
            .join(IndexedLabel, IndexedLabel.entity_id == IndexedEntity.id)
            .where(IndexedEntity.project_id == project_id, IndexedEntity.branch == branch)
        )
        labels = [LabelValue(*row) for row in label_result.all()]
        record_result = await self._db.execute(
            select(TranslationRecord).where(TranslationRecord.project_id == project_id)
        )
        records = list(record_result.scalars().all())
        repository = self._git.get_repository(project_id)
        candidates = [
            path
            for path in repository.list_files(branch)
            if path.casefold().endswith((".ttl", ".owl", ".rdf"))
        ]
        filename = "ontology.ttl" if "ontology.ttl" in candidates else candidates[0]
        graph = Graph().parse(
            data=self._git.get_file_from_branch(project_id, branch, filename), format="turtle"
        )
        return list(config.language_tags if config else []), labels, records, graph

    async def _pending_scopes(
        self,
        project_id: UUID,
        branch: str,
        candidates: set[tuple[str, str, str]],
    ) -> set[tuple[str, str, str]]:
        """Return candidate slots covered by an active project backfill."""
        if self._db is None:
            return set()
        active_states = ("pending", "running")
        result = await self._db.execute(
            select(TranslationJob).where(
                TranslationJob.project_id == project_id,
                TranslationJob.branch == branch,
                TranslationJob.status.in_(active_states),
            )
        )
        return self.pending_scopes_from_jobs(list(result.scalars().all()), candidates)

    @staticmethod
    def pending_scopes_from_jobs(
        jobs: list[TranslationJob], candidates: set[tuple[str, str, str]]
    ) -> set[tuple[str, str, str]]:
        """Expand project-level jobs over current candidate slots and language filters."""
        return {
            scope
            for job in jobs
            for scope in candidates
            if job.language is None or scope[2].casefold() == job.language.casefold()
        }

    @classmethod
    def classify(
        cls,
        branch: str,
        languages: list[str],
        labels: list[LabelValue],
        records: list[TranslationRecord],
        graph: Graph,
        pending: set[tuple[str, str, str]] | None = None,
    ) -> dict[str, Any]:
        slots = cls._source_slots(labels, records)
        states = cls._states(slots, languages, labels, records, graph, pending or set())
        rows = []
        for language in languages:
            counts = dict.fromkeys(("verified", "provisional", "pending", "missing"), 0)
            for entity_iri, predicate in slots:
                counts[states[(entity_iri, predicate, language)][0]] += 1
            rows.append({"language": language, **counts, "total": len(slots)})
        return {
            "branch": branch,
            "languages": rows,
            "total_entities": len({entity for entity, _ in slots}),
        }

    @classmethod
    def entity_state_from_data(
        cls,
        entity_iri: str,
        branch: str,
        languages: list[str],
        labels: list[LabelValue],
        records: list[TranslationRecord],
        graph: Graph,
        pending: set[tuple[str, str, str]] | None = None,
    ) -> dict[str, Any]:
        slots = {slot for slot in cls._source_slots(labels, records) if slot[0] == entity_iri}
        states = cls._states(slots, languages, labels, records, graph, pending or set())
        items = []
        for entity, predicate in sorted(slots):
            for language in languages:
                state, record = states[(entity, predicate, language)]
                items.append(
                    {
                        "predicate": predicate,
                        "language": language,
                        "state": state,
                        "value": record.proposed_value
                        if state == "provisional" and record
                        else None,
                        "record_id": str(record.id) if record else None,
                    }
                )
        return {"entity_iri": entity_iri, "branch": branch, "items": items}

    @classmethod
    def provisional_from_data(
        cls, language: str, labels: list[LabelValue], records: list[TranslationRecord]
    ) -> list[dict[str, Any]]:
        current_hashes = {
            (label.entity_iri, label.predicate, label.value_hash) for label in labels
        }
        rows = []
        for record in records:
            if record.language != language or record.state != "provisional":
                continue
            if record.source_value is None or record.proposed_value is None:
                continue
            if (
                record.entity_iri,
                record.predicate,
                record.source_value_hash,
            ) not in current_hashes:
                continue
            rows.append(
                {
                    "record_id": str(record.id),
                    "entity_iri": record.entity_iri,
                    "predicate": record.predicate,
                    "language": record.language,
                    "source_value": record.source_value,
                    "proposed_value": record.proposed_value,
                    "model_name": record.model_name,
                    "method": record.method,
                    "score": record.score,
                    "created_at": record.created_at,
                }
            )
        return sorted(rows, key=lambda row: (row["created_at"], row["record_id"]))

    @staticmethod
    def _source_slots(
        labels: list[LabelValue], records: list[TranslationRecord]
    ) -> set[tuple[str, str]]:
        translated = {
            (record.entity_iri, record.predicate, record.language, record.translated_value_hash)
            for record in records
        }
        return {
            (label.entity_iri, label.predicate)
            for label in labels
            if (label.entity_iri, label.predicate, label.language, label.value_hash)
            not in translated
        }

    @classmethod
    def _states(
        cls,
        slots: set[tuple[str, str]],
        languages: list[str],
        labels: list[LabelValue],
        records: list[TranslationRecord],
        graph: Graph,
        pending: set[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], tuple[str, TranslationRecord | None]]:
        label_lookup = {
            (
                label.entity_iri,
                label.predicate,
                label.language,
                label.value_hash,
            ): label
            for label in labels
        }
        source_hashes = {
            (label.entity_iri, label.predicate, label.value_hash) for label in labels
        }
        records_by_slot: dict[tuple[str, str, str], list[TranslationRecord]] = {}
        for record in records:
            records_by_slot.setdefault(
                (record.entity_iri, record.predicate, record.language), []
            ).append(record)
        output: dict[tuple[str, str, str], tuple[str, TranslationRecord | None]] = {}
        for entity, predicate in slots:
            for language in languages:
                key = (entity, predicate, language)
                candidates = [
                    record
                    for record in records_by_slot.get(key, [])
                    if (entity, predicate, record.source_value_hash) in source_hashes
                ]
                verified = next(
                    (
                        record
                        for record in candidates
                        if cls._is_verified(record, label_lookup, graph)
                    ),
                    None,
                )
                provisional = next(
                    (
                        record
                        for record in candidates
                        if record.state == "provisional" and record.proposed_value is not None
                    ),
                    None,
                )
                if verified:
                    output[key] = ("verified", verified)
                elif provisional:
                    output[key] = ("provisional", provisional)
                elif key in pending:
                    output[key] = ("pending", None)
                else:
                    output[key] = ("missing", None)
        return output

    @staticmethod
    def _is_verified(
        record: TranslationRecord,
        labels: dict[tuple[str, str, str | None, str], LabelValue],
        graph: Graph,
    ) -> bool:
        if record.state != "verified":
            return False
        label = labels.get(
            (record.entity_iri, record.predicate, record.language, record.translated_value_hash)
        )
        if label is None:
            return False
        literal = Literal(label.value, lang=label.language)
        annotation = read_annotation(
            graph, URIRef(record.entity_iri), URIRef(record.predicate), literal
        )
        return annotation is not None and annotation.record_digest == translation_record_digest(
            record
        )


__all__ = ["LabelValue", "TranslationCoverageService"]
