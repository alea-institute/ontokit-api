"""Structural contract for the change origin and generated entity migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, call

import sqlalchemy as sa

from ontokit.models.change_event import EntityChangeEvent

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "j8k9l0m1n2o3_add_change_origin_and_llm_generated_entities.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("change_origin_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_change_origin_and_generated_entity_ledger() -> None:
    module = _load_migration()
    assert module.revision == "j8k9l0m1n2o3"
    assert module.down_revision == "i7j8k9l0m1n2"
    module.op = fake_op = MagicMock()

    module.upgrade()

    columns = {}
    for entry in fake_op.add_column.call_args_list:
        table, column = entry.args
        assert table == "entity_change_events"
        columns[column.name] = column
    assert set(columns) == {"origin", "origin_model", "origin_batch_id"}
    assert columns["origin"].type.length == 20
    assert not columns["origin"].nullable
    assert str(columns["origin"].server_default.arg) == "human"
    assert columns["origin_model"].type.length == 255
    assert columns["origin_model"].nullable
    assert isinstance(columns["origin_batch_id"].type, sa.Uuid)
    assert columns["origin_batch_id"].nullable

    fake_op.create_table.assert_called_once()
    table, *ledger_columns = fake_op.create_table.call_args.args
    assert table == "llm_generated_entities"
    ledger = sa.Table(table, sa.MetaData(), *ledger_columns)
    assert set(ledger.columns.keys()) == {
        "id",
        "project_id",
        "branch",
        "entity_iri",
        "batch_id",
        "suggestion_type",
        "focus_iri",
        "label",
        "definition",
        "model",
        "prompt_template",
        "user_id",
        "created_at",
    }
    for name in ("id", "project_id", "batch_id"):
        assert isinstance(ledger.c[name].type, sa.Uuid)
    assert ledger.c.id.primary_key
    (project_fk,) = ledger.c.project_id.foreign_keys
    assert project_fk.target_fullname == "projects.id"
    assert project_fk.ondelete == "CASCADE"
    for name, length in {
        "branch": 255,
        "entity_iri": 2000,
        "suggestion_type": 20,
        "focus_iri": 2000,
        "model": 255,
        "prompt_template": 50,
        "user_id": 255,
    }.items():
        assert ledger.c[name].type.length == length
    assert isinstance(ledger.c.label.type, sa.Text)
    assert isinstance(ledger.c.definition.type, sa.Text)
    assert {c.name for c in ledger.c if c.nullable} == {"definition", "model", "prompt_template"}
    assert ledger.c.created_at.type.timezone
    assert str(ledger.c.created_at.server_default.arg) == "now()"

    assert fake_op.create_index.call_args_list == [
        call(
            "ix_change_events_project_origin_time",
            "entity_change_events",
            ["project_id", "origin", "created_at"],
            postgresql_concurrently=True,
        ),
        call("ix_llm_generated_project_iri", table, ["project_id", "entity_iri"]),
        call("ix_llm_generated_project_batch", table, ["project_id", "batch_id"]),
    ]
    fake_op.get_context.assert_called_once_with()
    fake_op.assert_has_calls(
        [
            call.get_context().autocommit_block(),
            call.get_context().autocommit_block().__enter__(),
            call.create_index(
                "ix_change_events_project_origin_time",
                "entity_change_events",
                ["project_id", "origin", "created_at"],
                postgresql_concurrently=True,
            ),
            call.get_context().autocommit_block().__exit__(None, None, None),
        ]
    )
    event_table = sa.inspect(EntityChangeEvent).local_table
    for name, column in columns.items():
        assert str(event_table.c[name].type) == str(column.type)
        assert event_table.c[name].nullable == column.nullable
    assert str(event_table.c.origin.server_default.arg) == "human"
    index = next(i for i in event_table.indexes if i.name == "ix_change_events_project_origin_time")
    assert list(index.columns.keys()) == ["project_id", "origin", "created_at"]


def test_downgrade_removes_both_storage_additions() -> None:
    module = _load_migration()
    module.op = fake_op = MagicMock()

    module.downgrade()

    assert {entry.args[0] for entry in fake_op.drop_index.call_args_list} == {
        "ix_change_events_project_origin_time",
        "ix_llm_generated_project_iri",
        "ix_llm_generated_project_batch",
    }
    fake_op.get_context.assert_called_once_with()
    fake_op.assert_has_calls(
        [
            call.get_context().autocommit_block(),
            call.get_context().autocommit_block().__enter__(),
            call.drop_index(
                "ix_change_events_project_origin_time",
                table_name="entity_change_events",
                postgresql_concurrently=True,
            ),
            call.get_context().autocommit_block().__exit__(None, None, None),
        ]
    )
    fake_op.drop_table.assert_called_once_with("llm_generated_entities")
    assert {entry.args for entry in fake_op.drop_column.call_args_list} == {
        ("entity_change_events", "origin"),
        ("entity_change_events", "origin_model"),
        ("entity_change_events", "origin_batch_id"),
    }
