"""Migration contract for atomically tracking branch history progress."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, call

import sqlalchemy as sa

MIGRATION = (
    Path(__file__).parents[2] / "alembic/versions/k9l0m1n2o3p4_add_change_history_watermarks.py"
)
TABLE = "change_history_watermarks"
EVENTS = "entity_change_events"
INDEX = "ix_change_events_project_branch_commit"


def test_watermark_schema_and_concurrent_index() -> None:
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from ontokit.models import ChangeHistoryWatermark, EntityChangeEvent

    module.op = op = MagicMock()
    assert module.down_revision == "j8k9l0m1n2o3"
    module.upgrade()

    table_name, *elements = op.create_table.call_args.args
    assert table_name == TABLE
    assert op.create_table.call_args.kwargs == {"if_not_exists": True}
    table = sa.Table(TABLE, sa.MetaData(), *elements)
    assert list(table.primary_key.columns.keys()) == ["project_id", "branch"]
    assert isinstance(table.c.project_id.type, sa.Uuid)
    foreign_key = next(iter(table.c.project_id.foreign_keys))
    assert (foreign_key.target_fullname, foreign_key.ondelete) == ("projects.id", "CASCADE")
    assert table.c.branch.type.length == 255
    assert table.c.commit_hash.type.length == 40
    assert all(not column.nullable for column in table.c)
    assert table.c.updated_at.type.timezone
    assert str(table.c.updated_at.server_default.arg) == "now()"
    model_table = sa.inspect(ChangeHistoryWatermark).local_table
    assert list(model_table.primary_key.columns.keys()) == ["project_id", "branch"]
    assert list(model_table.columns.keys()) == list(table.columns.keys())
    op.assert_has_calls(
        [
            call.get_context().autocommit_block().__enter__(),
            # An interrupted build leaves an invalid index that a rerun must replace.
            call.drop_index(INDEX, table_name=EVENTS, postgresql_concurrently=True, if_exists=True),
            call.create_index(
                INDEX,
                EVENTS,
                ["project_id", "branch", "commit_hash"],
                postgresql_concurrently=True,
            ),
            call.get_context().autocommit_block().__exit__(None, None, None),
        ]
    )
    index_name, event_table_name, index_columns = op.create_index.call_args.args
    event_table = sa.inspect(EntityChangeEvent).local_table
    assert event_table.name == event_table_name
    event_indexes = {index.name: list(index.columns.keys()) for index in event_table.indexes}
    assert event_indexes.get(index_name) == index_columns

    op.reset_mock()
    module.downgrade()
    op.assert_has_calls(
        [
            call.get_context().autocommit_block().__enter__(),
            call.drop_index(INDEX, table_name=EVENTS, postgresql_concurrently=True, if_exists=True),
            call.get_context().autocommit_block().__exit__(None, None, None),
            call.drop_table(TABLE),
        ]
    )


ENTITY_MIGRATION = (
    Path(__file__).parents[2] / "alembic/versions/l0m1n2o3p4q5_index_change_events_by_entity.py"
)
ENTITY_INDEX = "ix_change_events_project_branch_entity_time"


def test_entity_history_index_matches_the_model_and_builds_concurrently() -> None:
    spec = importlib.util.spec_from_file_location(ENTITY_MIGRATION.stem, ENTITY_MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from ontokit.models import EntityChangeEvent

    module.op = op = MagicMock()
    assert module.down_revision == "77a67de3294e"
    module.upgrade()

    columns = ["project_id", "branch", "entity_iri", "created_at", "id"]
    op.assert_has_calls(
        [
            call.get_context().autocommit_block().__enter__(),
            call.drop_index(
                ENTITY_INDEX, table_name=EVENTS, postgresql_concurrently=True, if_exists=True
            ),
            call.create_index(
                ENTITY_INDEX,
                EVENTS,
                columns,
                postgresql_include=["event_type", "origin", "recorded_xact"],
                postgresql_concurrently=True,
            ),
            call.get_context().autocommit_block().__exit__(None, None, None),
        ]
    )
    (model_index,) = (
        index
        for index in sa.inspect(EntityChangeEvent).local_table.indexes
        if index.name == ENTITY_INDEX
    )
    assert [column.name for column in model_index.columns] == columns
    assert model_index.dialect_options["postgresql"]["include"] == [
        "event_type",
        "origin",
        "recorded_xact",
    ]

    op.reset_mock()
    module.downgrade()
    op.drop_index.assert_called_once_with(
        ENTITY_INDEX, table_name=EVENTS, postgresql_concurrently=True, if_exists=True
    )


TRANSACTION_MIGRATION = (
    Path(__file__).parents[2] / "alembic/versions/77a67de3294e_record_change_event_transactions.py"
)


def test_recorded_transaction_column_is_added_without_a_table_rewrite() -> None:
    spec = importlib.util.spec_from_file_location(TRANSACTION_MIGRATION.stem, TRANSACTION_MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from ontokit.models import EntityChangeEvent

    module.op = op = MagicMock()
    assert module.down_revision == "k9l0m1n2o3p4"
    module.upgrade()

    table_name, column = op.add_column.call_args.args
    assert (table_name, column.name, column.nullable) == (EVENTS, "recorded_xact", True)
    assert isinstance(column.type, sa.BigInteger)
    # A default on the added column would fill every existing row.
    assert column.server_default is None
    (table_name, column_name), options = op.alter_column.call_args
    assert (table_name, column_name) == (EVENTS, "recorded_xact")
    assert str(options["server_default"]) == ("CAST(CAST(pg_current_xact_id() AS text) AS bigint)")
    model_column = sa.inspect(EntityChangeEvent).local_table.c.recorded_xact
    assert model_column.nullable and isinstance(model_column.type, sa.BigInteger)

    op.reset_mock()
    module.downgrade()
    op.drop_column.assert_called_once_with(EVENTS, "recorded_xact")
