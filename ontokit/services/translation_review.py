"""Native-speaker review transitions and their ontology commits."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from rdflib import Graph, Literal, URIRef
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.constants import ONTOKIT_COMMITTER_EMAIL, ONTOKIT_COMMITTER_NAME
from ontokit.git.bare_repository import BareGitRepositoryService, CommitInfo
from ontokit.models.project import ProjectMember
from ontokit.models.translation import TranslationRecord, hash_literal_value
from ontokit.services.branch_lock import branch_write_lock
from ontokit.services.translation_annotations import (
    TranslationAnnotation,
    annotate,
    read_annotation,
    remove_annotation,
    translation_record_digest,
)
from ontokit.services.translation_index import enqueue_ontology_index

logger = logging.getLogger(__name__)
IndexEnqueuer = Callable[..., Awaitable[None]]


class TranslationReviewConflict(RuntimeError):
    """The branch no longer contains the record's exact reviewable source/target state."""


class TranslationReviewService:
    def __init__(
        self,
        db: AsyncSession,
        git_service: BareGitRepositoryService,
        *,
        index_enqueuer: IndexEnqueuer | None = None,
    ) -> None:
        self.db = db
        self.git = git_service
        self.index_enqueuer = index_enqueuer or enqueue_ontology_index

    @staticmethod
    def authorize(
        project_id: uuid.UUID,
        member: ProjectMember,
        reviewer_languages: set[str],
        record: TranslationRecord,
    ) -> None:
        if member.project_id != project_id or record.project_id != project_id:
            raise PermissionError("translation record is outside this project")
        if record.language.casefold() not in {tag.casefold() for tag in reviewer_languages}:
            raise PermissionError("native-reviewer tag required for this language")

    async def confirm_loaded(
        self,
        *,
        project_id: uuid.UUID,
        branch: str,
        filename: str,
        member: ProjectMember,
        reviewer_languages: set[str],
        record: TranslationRecord,
        author_name: str,
        author_email: str,
    ) -> CommitInfo:
        try:
            async with branch_write_lock(self.db, project_id, branch):
                await self.db.refresh(record, with_for_update=True)
                self.authorize(project_id, member, reviewer_languages, record)
                if record.proposed_value is None:
                    raise ValueError("translation record has no proposed literal")
                literal = Literal(record.proposed_value, lang=record.language)
                record.confirm(member.id)
                await self.db.flush()
                commit = self._commit_graph_change_locked(
                    project_id=project_id,
                    branch=branch,
                    filename=filename,
                    message=f"Confirm {record.language} translation",
                    author_name=author_name,
                    author_email=author_email,
                    mutate=lambda graph: self._confirm_graph(graph, record, literal),
                )
                await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        assert commit is not None
        await self._enqueue(project_id, branch, commit.hash)
        return commit

    async def reject_loaded(
        self,
        *,
        project_id: uuid.UUID,
        branch: str,
        filename: str,
        member: ProjectMember,
        reviewer_languages: set[str],
        record: TranslationRecord,
        author_name: str,
        author_email: str,
    ) -> CommitInfo | None:
        commit: CommitInfo | None = None
        try:
            async with branch_write_lock(self.db, project_id, branch):
                await self.db.refresh(record, with_for_update=True)
                self.authorize(project_id, member, reviewer_languages, record)
                if record.state != "provisional":
                    raise ValueError("only provisional translation records can be rejected")
                record.state = "rejected"
                await self.db.flush()
                if record.proposed_value is not None:
                    literal = Literal(record.proposed_value, lang=record.language)
                    commit = self._commit_graph_change_locked(
                        project_id=project_id,
                        branch=branch,
                        filename=filename,
                        message=f"Reject {record.language} translation",
                        author_name=author_name,
                        author_email=author_email,
                        mutate=lambda target: self._reject_graph(target, record, literal),
                        commit_if_unchanged=False,
                    )
                await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        if commit is not None:
            await self._enqueue(project_id, branch, commit.hash)
        return commit

    @staticmethod
    def _confirm_graph(graph: Graph, record: TranslationRecord, literal: Literal) -> None:
        subject, predicate = URIRef(record.entity_iri), URIRef(record.predicate)
        sources = [
            value
            for value in graph.objects(subject, predicate)
            if isinstance(value, Literal)
            and hash_literal_value(str(value)) == record.source_value_hash
        ]
        if not sources or not any(str(value) == record.source_value for value in sources):
            raise TranslationReviewConflict("translation source literal changed")
        existing = (subject, predicate, literal) in graph
        annotation = read_annotation(graph, subject, predicate, literal)
        if existing and (
            annotation is None or annotation.record_digest != translation_record_digest(record)
        ):
            raise TranslationReviewConflict(
                "translation target belongs to another author or record"
            )
        graph.add((subject, predicate, literal))
        annotate(
            graph,
            subject,
            predicate,
            literal,
            TranslationAnnotation(
                method=record.method,
                state="verified",
                created=record.created_at,
                record_digest=translation_record_digest(record),
            ),
        )

    @staticmethod
    def _reject_graph(graph: Graph, record: TranslationRecord, literal: Literal) -> None:
        subject, predicate = URIRef(record.entity_iri), URIRef(record.predicate)
        annotation = read_annotation(graph, subject, predicate, literal)
        if annotation is None or annotation.record_digest != translation_record_digest(record):
            return
        remove_annotation(graph, subject, predicate, literal)
        graph.remove((subject, predicate, literal))

    def _commit_graph_change_locked(
        self,
        *,
        project_id: uuid.UUID,
        branch: str,
        filename: str,
        message: str,
        author_name: str,
        author_email: str,
        mutate: Callable[[Graph], None],
        commit_if_unchanged: bool = True,
    ) -> CommitInfo | None:
        content = self.git.get_file_from_branch(project_id, branch, filename)
        graph = Graph().parse(data=content, format="turtle")
        mutate(graph)
        if not commit_if_unchanged and graph.isomorphic(
            Graph().parse(data=content, format="turtle")
        ):
            return None
        updated = graph.serialize(format="turtle").encode()
        return self.git.commit_changes(
            project_id=project_id,
            ontology_content=updated,
            filename=filename,
            message=message,
            author_name=author_name,
            author_email=author_email,
            branch_name=branch,
            committer_name=ONTOKIT_COMMITTER_NAME,
            committer_email=ONTOKIT_COMMITTER_EMAIL,
        )

    async def _enqueue(self, project_id: uuid.UUID, branch: str, commit_hash: str) -> None:
        try:
            await self.index_enqueuer(project_id=project_id, branch=branch, commit_hash=commit_hash)
        except Exception:
            logger.warning("Failed to queue native-review ontology re-index", exc_info=True)


__all__ = ["TranslationReviewConflict", "TranslationReviewService"]
