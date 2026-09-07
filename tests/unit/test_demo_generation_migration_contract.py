"""Migration safety contract for generation-atomic demo publication."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from ontokit.models.demo_generation import DemoGeneration

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "h6i7j8k9l0m1_add_atomic_demo_generations.py"
)
PURGE_MIGRATION = MIGRATION.with_name("i7j8k9l0m1n2_add_demo_generation_purge_marker.py")

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


def _load_migration(path: Path = MIGRATION) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
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


def test_purge_migration_is_the_single_head() -> None:
    module = _load_migration(PURGE_MIGRATION)
    config = Config()
    config.set_main_option("script_location", str(MIGRATION.parents[1]))

    assert module.down_revision == "h6i7j8k9l0m1"
    assert ScriptDirectory.from_config(config).get_heads() == [module.revision]


def test_purge_columns_match_model_and_are_nullable() -> None:
    module = _load_migration(PURGE_MIGRATION)
    fake_op = MagicMock()
    module.op = fake_op

    module.upgrade()

    calls = fake_op.add_column.call_args_list
    assert len(calls) == 2
    columns = {call.args[1].name: call.args[1] for call in calls}
    assert set(columns) == {"purged_at", "purge_receipt"}
    for call in calls:
        assert call.args[0] == "demo_generations"
    for name, column in columns.items():
        model_column = DemoGeneration.__table__.c[name]
        assert column.nullable is True
        assert model_column.nullable is True
        if name == "purged_at":
            assert isinstance(column.type, sa.DateTime)
            assert isinstance(model_column.type, sa.DateTime)
            assert column.type.timezone is True
            assert model_column.type.timezone is True
        else:
            assert isinstance(column.type, sa.Text)
            assert isinstance(model_column.type, sa.Text)


def test_purge_migration_round_trips_empty_generation_table() -> None:
    """Exercise real DDL locally; PostgreSQL full-chain verification is host-owned."""
    old_op = _upgrade_with_legacy_demos([])
    table = sa.Table(
        *old_op.create_table.call_args.args[:1],
        sa.MetaData(),
        *old_op.create_table.call_args.args[1:],
    )
    module = _load_migration(PURGE_MIGRATION)
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            table.create(connection)
            before = sa.inspect(connection).get_columns("demo_generations")
            module.op = Operations(MigrationContext.configure(connection))

            module.upgrade()

            columns = {
                column["name"]: column
                for column in sa.inspect(connection).get_columns("demo_generations")
            }
            assert set(columns) == {column["name"] for column in before} | {
                "purged_at",
                "purge_receipt",
            }
            assert columns["purged_at"]["nullable"] is True
            assert columns["purge_receipt"]["nullable"] is True

            module.downgrade()

            after = sa.inspect(connection).get_columns("demo_generations")
            assert [(c["name"], str(c["type"]), c["nullable"]) for c in after] == [
                (c["name"], str(c["type"]), c["nullable"]) for c in before
            ]
    finally:
        engine.dispose()
