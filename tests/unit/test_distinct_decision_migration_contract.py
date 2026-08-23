"""Structural contract for the distinct-decision audit migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "f5g6h7i8j9k0_make_distinct_decisions_auditable.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("distinct_decision_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_a_separate_table_without_mutating_legacy_rejections() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    module.op = fake_op

    module.upgrade()

    fake_op.create_table.assert_called_once()
    assert fake_op.create_table.call_args.args[0] == "distinct_entity_decisions"
    assert not fake_op.alter_column.called
    assert not fake_op.drop_column.called
    assert "duplicate_rejections" not in repr(fake_op.mock_calls)


def test_downgrade_fails_closed_when_audit_rows_exist() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.scalar_one.return_value = 1
    module.op = fake_op

    with pytest.raises(RuntimeError, match="contains audit data"):
        module.downgrade()

    fake_op.drop_table.assert_not_called()


def test_empty_audit_table_can_be_downgraded_safely() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.scalar_one.return_value = 0
    module.op = fake_op

    module.downgrade()

    fake_op.drop_table.assert_called_once_with("distinct_entity_decisions")
