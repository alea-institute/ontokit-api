"""Structural contract for the LLM audit keyset index migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "d3e4f5g6h7i8_add_llm_audit_keyset_index.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("llm_audit_keyset_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_and_downgrade_replace_the_expected_index() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    module.op = fake_op

    module.upgrade()
    fake_op.drop_index.assert_called_once_with(
        "ix_llm_audit_project_date",
        table_name="llm_audit_logs",
    )
    fake_op.create_index.assert_called_once_with(
        "ix_llm_audit_project_date",
        "llm_audit_logs",
        ["project_id", "created_at", "id"],
    )

    fake_op.reset_mock()
    module.downgrade()
    fake_op.create_index.assert_called_once_with(
        "ix_llm_audit_project_date",
        "llm_audit_logs",
        ["project_id", "created_at"],
    )
