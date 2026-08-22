"""Structural rollout contracts for embedding-integrity migrations."""

from __future__ import annotations

import ast
from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "c2d3e4f5g6h7_harden_embedding_index_integrity.py"
)


def _migration_source() -> str:
    source = MIGRATION.read_text(encoding="utf-8")
    ast.parse(source)
    return source


def test_upgrade_uses_validated_constraints_and_concurrent_hnsw_ddl() -> None:
    source = _migration_source()

    assert "NOT VALID" in source
    assert "VALIDATE CONSTRAINT ck_entity_embeddings_dimensions" in source
    assert "autocommit_block" in source
    assert source.count("CREATE INDEX CONCURRENTLY") >= 2
    assert "CREATE INDEX ix_entity_embeddings_hnsw_" not in source
    assert "halfvec_cosine_ops" in source
    assert "RAISE EXCEPTION" in source


def test_downgrade_restores_guarded_predecessor_hnsw_contract() -> None:
    source = _migration_source()

    assert "ix_entity_embeddings_hnsw" in source
    assert "embedding vector_cosine_ops" in source
    assert "EXCEPTION WHEN others" in source
    assert "RAISE WARNING" in source
