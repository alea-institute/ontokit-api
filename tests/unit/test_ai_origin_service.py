"""Tests for resolving and expiring generated entity provenance."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ontokit.models import LLMGeneratedEntity
from ontokit.services.ai_origin_service import AIOriginService

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_PROJECT_ID = UUID("22222222-2222-2222-2222-222222222222")
NAMESPACE = "https://example.org/ontology#"
CREATED_AT = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def ledger_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    LLMGeneratedEntity.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _ledger_row(
    name: str,
    *,
    project_id: UUID = PROJECT_ID,
    label: str = "Lease",
    definition: str | None = "A contract granting use.",
    model: str | None = "example-model",
    created_at: datetime = CREATED_AT,
) -> LLMGeneratedEntity:
    return LLMGeneratedEntity(
        project_id=project_id,
        branch="suggest/test",
        entity_iri=NAMESPACE + name,
        batch_id=uuid4(),
        suggestion_type="children",
        focus_iri=NAMESPACE + "Contract",
        label=label,
        definition=definition,
        model=model,
        user_id="test-user",
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_resolve_classifies_generated_entities_in_one_query(ledger_session: Session) -> None:
    unchanged = _ledger_row("Unchanged")
    edited_label = _ledger_row("EditedLabel")
    edited_definition = _ledger_row("EditedDefinition")
    no_definition = _ledger_row("NoDefinition", definition=None, model=None)
    whitespace = _ledger_row(
        "Whitespace", label="  Property\tLease  ", definition="A contract\n granting use. "
    )
    older = _ledger_row("Repeated")
    newest = _ledger_row(
        "Repeated", label="New Lease", model="new-model", created_at=CREATED_AT + timedelta(days=1)
    )
    other_project = _ledger_row("OtherProject", project_id=OTHER_PROJECT_ID)
    newer_other_project = _ledger_row(
        "Unchanged",
        project_id=OTHER_PROJECT_ID,
        created_at=CREATED_AT + timedelta(days=1),
    )
    ledger_session.add_all(
        [
            unchanged,
            edited_label,
            edited_definition,
            no_definition,
            whitespace,
            older,
            newest,
            other_project,
            newer_other_project,
        ]
    )
    ledger_session.flush()
    db = AsyncMock(spec=AsyncSession)
    db.scalars.side_effect = ledger_session.scalars

    resolved = await AIOriginService(db).resolve(
        PROJECT_ID,
        {
            unchanged.entity_iri: {
                "labels": ["Rental", "Lease"],
                "definitions": ["Use of property.", "A contract granting use."],
            },
            edited_label.entity_iri: {
                "labels": ["lease"],
                "definitions": ["A contract granting use."],
            },
            edited_definition.entity_iri: {
                "labels": ["Lease"],
                "definitions": ["An agreement granting use."],
            },
            no_definition.entity_iri: {"labels": ["Lease"], "definitions": ["Added later."]},
            whitespace.entity_iri: {
                "labels": ["Property Lease"],
                "definitions": [" A contract granting  use. "],
            },
            newest.entity_iri: {"labels": ["Lease"], "definitions": ["A contract granting use."]},
            other_project.entity_iri: {
                "labels": ["Lease"],
                "definitions": ["A contract granting use."],
            },
            NAMESPACE + "Unknown": {"labels": [], "definitions": []},
        },
    )

    db.scalars.assert_awaited_once()
    # Entries on the suggestion branch still match when the entity lands elsewhere.
    assert resolved == {
        row.entity_iri: {"origin": origin, "model": row.model, "batch_id": str(row.batch_id)}
        for row, origin in (
            (unchanged, "ai"),
            (edited_label, "ai_edited"),
            (edited_definition, "ai_edited"),
            (no_definition, "ai"),
            (whitespace, "ai"),
            (newest, "ai_edited"),
        )
    }


@pytest.mark.asyncio
async def test_resolve_empty_input_skips_query() -> None:
    db = AsyncMock(spec=AsyncSession)

    assert await AIOriginService(db).resolve(PROJECT_ID, {}) == {}
    db.execute.assert_not_awaited()
    db.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_older_than_deletes_only_expired_rows(ledger_session: Session) -> None:
    now = datetime.now(UTC)
    expired = _ledger_row("Expired", created_at=now - timedelta(days=31))
    recent = _ledger_row("Recent", created_at=now - timedelta(days=29))
    ledger_session.add_all([expired, recent])
    ledger_session.flush()
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = ledger_session.execute
    service = AIOriginService(db)

    assert await service.purge_older_than(30) == 1
    assert list(ledger_session.scalars(select(LLMGeneratedEntity))) == [recent]
    assert await service.purge_older_than(30) == 0
    db.commit.assert_not_awaited()
