"""Vector write and ANN-index integrity regressions."""

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from ontokit.models.embedding import EntityEmbedding, EntityEmbeddingStaging
from ontokit.services.embedding_service import (
    _active_embedding_upsert,
    _distance_operands,
    _staged_embedding_upsert,
    _staged_snapshot_activation,
    _validate_embedding_dimensions,
)

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
JOB_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


def _values() -> dict[str, object]:
    return {
        "project_id": PROJECT_ID,
        "branch": "main",
        "entity_iri": "http://example.org/Entity",
        "entity_type": "class",
        "label": "Entity",
        "embedding_text": "Entity definition",
        "embedding": [0.1, 0.2, 0.3],
        "dimensions": 3,
        "provider": "local",
        "model_name": "test-model",
        "deprecated": False,
    }


def test_embedding_models_record_dimensions_and_stage_by_job() -> None:
    assert EntityEmbedding.__table__.c.dimensions.nullable is False
    active_checks = {
        constraint.name
        for constraint in EntityEmbedding.__table__.constraints
        if constraint.name is not None
    }
    assert "ck_entity_embeddings_dimensions" in active_checks
    staging = EntityEmbeddingStaging.__table__
    assert staging.c.dimensions.nullable is False
    assert {column.name for column in staging.primary_key.columns} == {"job_id", "entity_iri"}
    staging_checks = {
        constraint.name for constraint in staging.constraints if constraint.name is not None
    }
    assert {
        "ck_entity_embedding_staging_dimensions",
        "ck_entity_embedding_staging_vector_dimensions",
    } <= staging_checks


def test_dimension_validation_rejects_provider_contract_drift() -> None:
    _validate_embedding_dimensions([0.1, 0.2, 0.3], expected=3)

    with pytest.raises(ValueError, match="expected 3 dimensions, received 2"):
        _validate_embedding_dimensions([0.1, 0.2], expected=3)


@pytest.mark.parametrize(
    ("dimensions", "column_operand", "query_operand"),
    [
        (384, "embedding::vector(384)", "CAST(:query_vec AS vector(384))"),
        (1536, "embedding::vector(1536)", "CAST(:query_vec AS vector(1536))"),
        (3072, "embedding::halfvec(3072)", "CAST(:query_vec AS halfvec(3072))"),
        (777, "embedding", "CAST(:query_vec AS vector)"),
    ],
)
def test_distance_operands_match_real_partial_hnsw_indexes(
    dimensions: int, column_operand: str, query_operand: str
) -> None:
    assert _distance_operands(dimensions) == (column_operand, query_operand)


def test_active_upsert_is_conflict_safe_and_carries_dimensions() -> None:
    statement = _active_embedding_upsert(_values())
    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]

    assert "ON CONFLICT ON CONSTRAINT uq_entity_embedding DO UPDATE" in sql
    assert "dimensions" in sql


def test_staging_upsert_is_retry_safe_per_job_and_entity() -> None:
    values = {"job_id": JOB_ID, **_values()}
    statement = _staged_embedding_upsert([values])
    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]

    assert "ON CONFLICT (job_id, entity_iri) DO UPDATE" in sql
    assert "dimensions" in sql


def test_snapshot_activation_generates_a_distinct_server_side_id_per_row() -> None:
    statement = _staged_snapshot_activation(JOB_ID)
    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]

    assert "gen_random_uuid()" in sql
    assert "WHERE entity_embedding_staging.job_id" in sql
    assert "%(id)s" not in sql
