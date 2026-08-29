"""Migration safety contract for generation-atomic demo publication."""

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
    / "h6i7j8k9l0m1_add_atomic_demo_generations.py"
)

APPROVED_LEGACY_DEMOS = [
    (
        "alea-institute",
        "folio",
        "alea-institute",
        "ontokit-demo-folio",
        True,
    ),
    (
        "catholicos",
        "ontology-semantic-canon",
        "alea-institute",
        "ontokit-demo-semantic-canon",
        True,
    ),
]


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("atomic_demo_generation_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_with_legacy_demos(rows: list[tuple[object, ...]]) -> MagicMock:
    module = _load_migration()
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.all.return_value = rows
    module.op = fake_op  # type: ignore[attr-defined]
    module.upgrade()
    return fake_op


def test_migration_replaces_single_demo_with_per_generation_uniqueness() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision: str | None = "g5h6i7j8k9l0"' in source
    assert '"demo_generations"' in source
    assert '"uq_demo_generations_single_active"' in source
    assert 'sa.Column("attempt_token", sa.Uuid(), nullable=False)' in source
    assert '"demo_generation_id"' in source
    assert '"demo_commit_hash"' in source
    assert '"uq_projects_demo_source_generation"' in source
    assert 'op.drop_constraint("uq_projects_demo_source_project_id"' in source


def test_downgrade_refuses_to_collapse_multiple_generations() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.first.return_value = object()
    module.op = fake_op  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="multiple generations exist"):
        module.downgrade()

    fake_op.drop_constraint.assert_not_called()
    fake_op.drop_column.assert_not_called()


def test_upgrade_refuses_a_partially_visible_legacy_generation() -> None:
    module = _load_migration()
    fake_op = MagicMock()
    rows = [APPROVED_LEGACY_DEMOS[0], (*APPROVED_LEGACY_DEMOS[1][:-1], False)]
    fake_op.get_bind.return_value.execute.return_value.all.return_value = rows
    module.op = fake_op  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="partially published legacy demo set"):
        module.upgrade()

    fake_op.drop_constraint.assert_not_called()


@pytest.mark.parametrize(
    "rows",
    [
        APPROVED_LEGACY_DEMOS[:1],
        [*APPROVED_LEGACY_DEMOS, APPROVED_LEGACY_DEMOS[0]],
    ],
    ids=["one-demo", "three-demos"],
)
def test_upgrade_refuses_an_incomplete_or_duplicated_legacy_demo_set(
    rows: list[tuple[object, ...]],
) -> None:
    with pytest.raises(RuntimeError, match="exactly the approved"):
        _upgrade_with_legacy_demos(rows)


def test_upgrade_refuses_a_legacy_demo_with_an_unapproved_target() -> None:
    rows = [
        APPROVED_LEGACY_DEMOS[0],
        (
            "catholicos",
            "ontology-semantic-canon",
            "alea-institute",
            "ontokit-demo-unapproved",
            True,
        ),
    ]

    with pytest.raises(RuntimeError, match="exactly the approved"):
        _upgrade_with_legacy_demos(rows)


def test_upgrade_refuses_approved_repositories_with_wrong_source_correlation() -> None:
    rows = [
        (
            "alea-institute",
            "folio",
            "alea-institute",
            "ontokit-demo-semantic-canon",
            True,
        ),
        (
            "catholicos",
            "ontology-semantic-canon",
            "alea-institute",
            "ontokit-demo-folio",
            True,
        ),
    ]

    with pytest.raises(RuntimeError, match="correlated GitHub integrations"):
        _upgrade_with_legacy_demos(rows)


def test_upgrade_accepts_exactly_the_approved_uniform_legacy_demo_set() -> None:
    fake_op = _upgrade_with_legacy_demos(APPROVED_LEGACY_DEMOS)

    fake_op.drop_constraint.assert_called_once_with(
        "uq_projects_demo_source_project_id", "projects", type_="unique"
    )
