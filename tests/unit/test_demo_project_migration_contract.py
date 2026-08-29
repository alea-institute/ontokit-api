"""Downgrade safety contract for demo-project identity and linkage."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, call

import pytest

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "d2e3f4g5h6i7_add_demo_project_isolation.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_project_isolation_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_downgrade_refuses_populated_demo_identity_or_linkage() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.scalar_one.return_value = True
    module.op = fake_op  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="archive or remove demo identity and linkage"):
        module.downgrade()

    statement = str(fake_op.get_bind.return_value.execute.call_args.args[0])
    assert "is_demo IS TRUE" in statement
    assert "demo_source_project_id IS NOT NULL" in statement
    fake_op.drop_constraint.assert_not_called()
    fake_op.drop_column.assert_not_called()


def test_empty_demo_identity_and_linkage_downgrades_in_dependency_order() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.scalar_one.return_value = False
    module.op = fake_op  # type: ignore[attr-defined]

    module.downgrade()

    assert fake_op.mock_calls[-4:] == [
        call.drop_constraint("uq_projects_demo_source_project_id", "projects", type_="unique"),
        call.drop_constraint("fk_projects_demo_source_project_id", "projects", type_="foreignkey"),
        call.drop_column("projects", "demo_source_project_id"),
        call.drop_column("projects", "is_demo"),
    ]
