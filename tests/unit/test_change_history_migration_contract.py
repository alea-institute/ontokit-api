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
