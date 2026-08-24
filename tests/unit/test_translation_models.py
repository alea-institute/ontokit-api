"""Tests for durable machine-translation provenance records."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ontokit.core.database import Base
from ontokit.models.project import Project, ProjectMember
from ontokit.models.translation import TranslationRecord, hash_literal_value


@pytest.fixture
def translation_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Project.__table__,
            ProjectMember.__table__,
            TranslationRecord.__table__,
        ],
    )
    with Session(engine) as session:
        yield session
    engine.dispose()


def _project(session: Session) -> Project:
    project = Project(name="Translations", owner_id="owner")
    session.add(project)
    session.flush()
    return project


def _record(project_id: uuid.UUID, **overrides: object) -> TranslationRecord:
    values: dict[str, object] = {
        "project_id": project_id,
        "entity_iri": "https://example.org/concept",
        "predicate": "http://www.w3.org/2000/01/rdf-schema#label",
        "language": "es",
        "source_value_hash": hash_literal_value(" Cathedral "),
        "translated_value_hash": hash_literal_value("Catedral"),
        "model_name": "translator",
        "model_version": "2026-01",
        "method": "consensus",
        "score": 0.96,
        "state": "provisional",
    }
    values.update(overrides)
    return TranslationRecord(**values)  # type: ignore[arg-type]


def test_round_trip_by_content_key_and_reject_exact_duplicate(
    translation_session: Session,
) -> None:
    project = _project(translation_session)
    record = _record(project.id)
    translation_session.add(record)
    translation_session.commit()

    found = translation_session.scalar(
        select(TranslationRecord).where(
            TranslationRecord.project_id == project.id,
            TranslationRecord.entity_iri == record.entity_iri,
            TranslationRecord.predicate == record.predicate,
            TranslationRecord.language == record.language,
            TranslationRecord.source_value_hash == record.source_value_hash,
            TranslationRecord.translated_value_hash == record.translated_value_hash,
        )
    )
    assert found is not None
    assert found.id == record.id

    translation_session.add(_record(project.id))
    with pytest.raises(IntegrityError):
        translation_session.commit()


def test_unconfirmed_machine_records_before_is_era_scoped(
    translation_session: Session,
) -> None:
    project = _project(translation_session)
    cutoff = datetime(2026, 6, 1, tzinfo=UTC)
    old_unconfirmed = _record(
        project.id,
        entity_iri="https://example.org/old",
        created_at=cutoff - timedelta(days=1),
    )
    new_unconfirmed = _record(
        project.id,
        entity_iri="https://example.org/new",
        created_at=cutoff + timedelta(days=1),
    )
    old_confirmed = _record(
        project.id,
        entity_iri="https://example.org/confirmed",
        created_at=cutoff - timedelta(days=2),
        confirmed_at=cutoff - timedelta(days=1),
    )
    other_project = Project(name="Other", owner_id="other")
    translation_session.add(other_project)
    translation_session.flush()
    other_old = _record(
        other_project.id,
        entity_iri="https://example.org/other",
        created_at=cutoff - timedelta(days=1),
    )
    translation_session.add_all([old_unconfirmed, new_unconfirmed, old_confirmed, other_old])
    translation_session.commit()

    records = translation_session.scalars(
        TranslationRecord.unconfirmed_machine_records_before(project.id, cutoff)
    ).all()

    assert records == [old_unconfirmed]


def test_confirm_provisional_record_stamps_member_and_timestamp(
    translation_session: Session,
) -> None:
    project = _project(translation_session)
    member = ProjectMember(project_id=project.id, user_id="reviewer", role="editor")
    record = _record(project.id)
    translation_session.add_all([member, record])
    translation_session.commit()
    confirmed_at = datetime(2026, 8, 9, 12, 30, tzinfo=UTC)

    record.confirm(member.id, at=confirmed_at)
    translation_session.commit()

    translation_session.refresh(record)
    assert record.state == "verified"
    assert record.confirming_member_id == member.id
    assert record.confirmed_at == confirmed_at.replace(tzinfo=None)


def test_invalid_state_is_rejected_by_create_all_schema(
    translation_session: Session,
) -> None:
    project = _project(translation_session)
    translation_session.add(_record(project.id, state="invented"))

    with pytest.raises(IntegrityError):
        translation_session.commit()


def test_hash_literal_value_normalizes_nfc_and_strips() -> None:
    assert hash_literal_value("  Cafe\N{COMBINING ACUTE ACCENT}  ") == hash_literal_value(
        "Caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    )
