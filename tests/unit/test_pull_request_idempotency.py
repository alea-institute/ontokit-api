"""Database contracts for pull-request source-branch idempotency."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from ontokit.models.pull_request import PullRequest

INDEX_NAME = "uq_pull_requests_open_source_branch"
MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "e4f5g6h7i8j9_add_open_pr_source_branch_index.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("open_pr_source_branch_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_declares_partial_unique_open_source_branch_index() -> None:
    index = next(index for index in PullRequest.__table__.indexes if index.name == INDEX_NAME)

    assert index.unique is True
    assert [column.name for column in index.columns] == ["project_id", "source_branch"]
    assert str(index.dialect_options["postgresql"]["where"]) == "status = 'open'"


def test_migration_creates_and_drops_partial_unique_index() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.all.return_value = []
    module.op = fake_op

    module.upgrade()
    create_call = fake_op.create_index.call_args
    assert create_call.args == (
        INDEX_NAME,
        "pull_requests",
        ["project_id", "source_branch"],
    )
    assert create_call.kwargs["unique"] is True
    assert str(create_call.kwargs["postgresql_where"]) == "status = 'open'"

    module.downgrade()
    fake_op.drop_index.assert_called_once_with(INDEX_NAME, table_name="pull_requests")


def test_migration_names_legacy_duplicate_groups_before_ddl() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.all.return_value = [
        ("11111111-1111-1111-1111-111111111111", "suggest/user-1", 2),
    ]
    module.op = fake_op

    with pytest.raises(RuntimeError, match=r"suggest/user-1 \(2\)"):
        module.upgrade()

    fake_op.create_index.assert_not_called()
