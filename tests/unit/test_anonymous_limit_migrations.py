"""Contracts for legacy-byte handling and the online reaper index."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"


def _load(filename: str, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_active_anonymous_sessions_are_conservatively_exhausted() -> None:
    module = _load(
        "f4g5h6i7j8k9_add_anonymous_content_bytes.py",
        "anonymous_content_bytes_migration",
    )
    fake_op = MagicMock()
    module.op = fake_op

    module.upgrade()

    statement = str(fake_op.execute.call_args.args[0])
    assert "anonymous_content_bytes = 262144000" in statement
    assert "is_anonymous IS true" in statement
    assert "status = 'active'" in statement


def test_reaper_index_is_created_and_dropped_concurrently() -> None:
    module = _load(
        "g5h6i7j8k9l0_add_stale_anonymous_reaper_index.py",
        "stale_anonymous_index_migration",
    )
    fake_op = MagicMock()
    module.op = fake_op

    module.upgrade()
    module.downgrade()

    assert module.down_revision == "f4g5h6i7j8k9"
    assert fake_op.get_context.return_value.autocommit_block.call_count == 2
    assert fake_op.create_index.call_args.kwargs["postgresql_concurrently"] is True
    assert fake_op.drop_index.call_args.kwargs["postgresql_concurrently"] is True
