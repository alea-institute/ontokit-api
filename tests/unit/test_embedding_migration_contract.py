"""Structural rollout contracts for embedding-integrity migrations."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

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


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("embedding_integrity_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_uses_validated_constraints_and_concurrent_hnsw_ddl() -> None:
    source = _migration_source()

    assert "NOT VALID" in source
    assert "VALIDATE CONSTRAINT ck_entity_embeddings_dimensions" in source
    assert "autocommit_block" in source
    assert source.count("CREATE INDEX CONCURRENTLY") >= 2
    assert "CREATE INDEX ix_entity_embeddings_hnsw_" not in source
    assert "halfvec_cosine_ops" in source
    assert "RAISE EXCEPTION" in source


def test_retry_drops_invalid_concurrent_index_before_rebuild() -> None:
    module = _load_migration()
    bind = MagicMock()
    bind.execute.return_value.scalar_one.return_value = True
    fake_op = MagicMock()
    fake_op.get_bind.return_value = bind
    module.op = fake_op

    module._drop_invalid_index("ix_entity_embeddings_hnsw_384")

    query = str(bind.execute.call_args.args[0])
    assert "i.indrelid = 'entity_embeddings'::regclass" in query
    assert "NOT i.indisvalid OR NOT i.indisready" in query
    assert bind.execute.call_args.args[1] == {"index_name": "ix_entity_embeddings_hnsw_384"}
    drop_sql = str(fake_op.execute.call_args.args[0])
    assert drop_sql == 'DROP INDEX CONCURRENTLY IF EXISTS "ix_entity_embeddings_hnsw_384"'


def test_downgrade_restores_guarded_predecessor_hnsw_contract() -> None:
    source = _migration_source()

    assert "ix_entity_embeddings_hnsw" in source
    assert "embedding vector_cosine_ops" in source
    assert "EXCEPTION WHEN others" in source
    assert "RAISE WARNING" in source
