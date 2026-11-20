"""Resolve class provenance against the server-owned generation ledger."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ontokit.models.llm_generated_entity import LLMGeneratedEntity


def _normalize(text: str) -> str:
    return " ".join(text.split())


class AIOriginService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def resolve(
        self, project_id: UUID, created: dict[str, dict[str, list[str]]]
    ) -> dict[str, dict[str, str | None]]:
        """Classify created entities using the newest proposal on any branch."""
        if not created:
            return {}

        ledger = LLMGeneratedEntity
        ranked = (
            select(
                ledger,
                func.row_number()
                .over(
                    partition_by=ledger.entity_iri,
                    order_by=(ledger.created_at.desc(), ledger.id.desc()),
                )
                .label("proposal_rank"),
            )
            .where(ledger.project_id == project_id, ledger.entity_iri.in_(list(created)))
            .subquery()
        )
        rows = await self._db.scalars(
            select(aliased(ledger, ranked)).where(ranked.c.proposal_rank == 1)
        )

        resolved: dict[str, dict[str, str | None]] = {}
        for row in rows:
            current = created[row.entity_iri]
            label_matches = _normalize(row.label) in map(_normalize, current["labels"])
            definition_matches = row.definition is None or _normalize(row.definition) in map(
                _normalize, current["definitions"]
            )
            resolved[row.entity_iri] = {
                "origin": "ai" if label_matches and definition_matches else "ai_edited",
                "model": row.model,
                "batch_id": str(row.batch_id),
            }
        return resolved

    async def purge_older_than(self, days: int) -> int:
        """Delete expired proposals in the caller's transaction."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = await self._db.execute(
            delete(LLMGeneratedEntity).where(LLMGeneratedEntity.created_at < cutoff)
        )
        return cast(CursorResult[Any], result).rowcount
