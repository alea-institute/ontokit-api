"""Schema contract for the generated entity ledger."""

from sqlalchemy import DateTime, String, Text, Uuid

from ontokit.models import LLMGeneratedEntity


def test_llm_generated_entity_table_contract() -> None:
    table = LLMGeneratedEntity.__table__
    assert table.name == "llm_generated_entities"
    expected = {
        "id": (Uuid, False, None),
        "project_id": (Uuid, False, None),
        "branch": (String, False, 255),
        "entity_iri": (String, False, 2000),
        "batch_id": (Uuid, False, None),
        "suggestion_type": (String, False, 20),
        "focus_iri": (String, False, 2000),
        "label": (Text, False, None),
        "definition": (Text, True, None),
        "model": (String, True, 255),
        "prompt_template": (String, True, 50),
        "user_id": (String, False, 255),
        "created_at": (DateTime, False, None),
    }
    assert set(table.columns.keys()) == expected.keys()
    for name, (column_type, nullable, length) in expected.items():
        column = table.columns[name]
        assert isinstance(column.type, column_type)
        assert column.nullable is nullable
        if length is not None:
            assert column.type.length == length
        if isinstance(column.type, Uuid):
            assert column.type.as_uuid
    assert list(table.primary_key.columns.keys()) == ["id"]
    (project_fk,) = table.columns.project_id.foreign_keys
    assert project_fk.target_fullname == "projects.id"
    assert project_fk.ondelete == "CASCADE"
    assert table.columns.created_at.type.timezone is True
    assert str(table.columns.created_at.server_default.arg) == "now()"
    assert {index.name: list(index.columns.keys()) for index in table.indexes} == {
        "ix_llm_generated_project_iri": ["project_id", "entity_iri"],
        "ix_llm_generated_project_batch": ["project_id", "batch_id"],
    }
