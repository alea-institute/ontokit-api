"""Structural contracts for GitHub mirror receipt migration safety."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "g6h7i8j9k0l1_add_pull_request_github_sync_receipts.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("github_sync_receipt_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_binds_legacy_receipts_to_their_current_repository() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    module.op = fake_op

    module.upgrade()

    statements = [str(call.args[0]) for call in fake_op.execute.call_args_list]
    identity_backfill = next(
        statement for statement in statements if "github_integration_id" in statement
    )
    assert "FROM github_integrations AS integration" in identity_backfill
    assert "integration.project_id = pr.project_id" in identity_backfill
    assert "pr.github_pr_number IS NOT NULL" in identity_backfill


def test_upgrade_marks_only_existing_legacy_mirrors_as_synced() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    module.op = fake_op

    module.upgrade()

    statements = [str(call.args[0]) for call in fake_op.execute.call_args_list]
    status_backfill = next(
        statement for statement in statements if "SET github_sync_status = 'synced'" in statement
    )
    assert "github_pr_number IS NOT NULL OR github_pr_url IS NOT NULL" in status_backfill
