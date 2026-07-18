"""Contract tests for normalized semantic-chunk classifier assignments."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import ForeignKeyConstraint, Index

from doc_store_server.db.schema import metadata
from doc_store_server.ingestion.runtime_boundary import _upsert_semantic_chunk_classifier_assignments


ROOT = Path(__file__).resolve().parents[3]
ASSIGNMENTS = {
    "semantic_chunk_type_assignments": ("chunk_type_id", "chunk_types", "type", "DocBlock"),
    "semantic_chunk_role_assignments": ("role_id", "chunk_roles", "role", "system"),
    "semantic_chunk_status_assignments": ("status_id", "chunk_statuses", "status", "new"),
    "semantic_chunk_block_type_assignments": ("block_type_id", "block_types", "block_type", "paragraph"),
    "semantic_chunk_language_assignments": ("language_id", "languages", "language", "UNKNOWN"),
    "semantic_chunk_category_assignments": ("category_id", "categories", "category", "uncategorized"),
}


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: object, params: dict[str, object]) -> None:
        self.calls.append((str(statement), params))


def _load_migration(filename: str, module_name: str) -> ModuleType:
    path = ROOT / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offline_sql(migration: ModuleType, function: str) -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        getattr(migration, function)()
    return output.getvalue()


def _index(table: object, name: str) -> Index:
    indexes = {index.name: index for index in table.indexes}  # type: ignore[attr-defined]
    assert name in indexes
    return indexes[name]


def test_metadata_has_chunk_owned_assignment_tables_with_dictionary_foreign_keys() -> None:
    assert set(ASSIGNMENTS) <= set(metadata.tables)
    for table_name, (column, dictionary_table, _metadata_field, _default_value) in ASSIGNMENTS.items():
        table = metadata.tables[table_name]
        assert set(table.c.keys()) == {
            "chunk_uuid",
            "chunk_version_id",
            column,
            "created_at",
            "updated_at",
        }
        assert set(table.primary_key.columns) == {table.c.chunk_uuid}
        assert table.c[column].nullable is False
        assert table.c.chunk_version_id.nullable is True
        actual = {
            (element.parent.name, element.column.table.name, element.column.name)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            for element in constraint.elements
        }
        assert actual == {
            ("chunk_uuid", "semantic_chunks", "id"),
            ("chunk_version_id", "semantic_chunk_versions", "id"),
            (column, dictionary_table, "id"),
        }
        assert any(element.ondelete == "CASCADE" for element in table.foreign_keys if element.parent.name == "chunk_uuid")
        assert any(element.ondelete == "SET NULL" for element in table.foreign_keys if element.parent.name == "chunk_version_id")
        assert set(_index(table, f"ix_{table_name}_{column}").columns) == {table.c[column]}
        assert set(_index(table, f"ix_{table_name}_chunk_version_id").columns) == {
            table.c.chunk_version_id,
        }


def test_0009_creates_assignment_tables_and_backfills_from_semantic_chunk_columns() -> None:
    migration = _load_migration("0009_semantic_chunk_classifier_assignments.py", "classifier_assignments")
    sql = _offline_sql(migration, "upgrade")

    for table_name, (column, dictionary_table, metadata_field, default_value) in ASSIGNMENTS.items():
        assert f"CREATE TABLE {table_name}" in sql
        assert "FOREIGN KEY(chunk_uuid) REFERENCES semantic_chunks (id) ON DELETE CASCADE" in sql
        assert f"FOREIGN KEY({column}) REFERENCES {dictionary_table} (id)" in sql
        assert f"CREATE INDEX ix_{table_name}_{column}" in sql
        assert f"SET {column} = dictionary.id" in sql
        assert f"sc.block_meta ->> '{metadata_field}'" in sql
        assert default_value in sql
        assert f"INSERT INTO {table_name} (chunk_uuid, {column})" in sql
        assert f"SELECT id, {column}" in sql
        assert "ON CONFLICT (chunk_uuid) DO NOTHING" in sql

    downgrade = _offline_sql(migration, "downgrade")
    assert "DROP TABLE semantic_chunk_type_assignments" in downgrade
    assert "DROP TABLE semantic_chunk_category_assignments" in downgrade


def test_0010_normalizes_legacy_text_category_to_uncategorized_default() -> None:
    migration = _load_migration("0010_normalize_text_category_default.py", "category_default_normalization")
    sql = _offline_sql(migration, "upgrade")

    assert "0010_normalize_text_category_default" == migration.revision
    assert migration.down_revision == "0009_semantic_chunk_classifier_assignments"
    assert "ON CONFLICT (descr) DO UPDATE" in sql
    assert "sc.category_id = category_ids.text_id" in sql
    assert "semantic_chunk_category_assignments AS assignment" in sql
    assert "category:uncategorized" in sql
    assert "category:text" in sql
    assert "to_jsonb('uncategorized'::text)" in sql


def test_runtime_write_upserts_pre_resolved_dictionary_ids_into_assignment_tables() -> None:
    connection = RecordingConnection()
    dictionary_ids = {column: uuid4() for column, _table, _metadata_field, _default_value in ASSIGNMENTS.values()}
    chunk_id = uuid4()

    _upsert_semantic_chunk_classifier_assignments(connection, chunk_id, dictionary_ids)

    assert len(connection.calls) == len(ASSIGNMENTS)
    for table_name, (column, _dictionary_table, _metadata_field, _default_value) in ASSIGNMENTS.items():
        assert any(
            f"INSERT INTO {table_name} (chunk_uuid, {column})" in sql
            and f"{column} = EXCLUDED.{column}" in sql
            and params == {"chunk_uuid": chunk_id, "dictionary_id": dictionary_ids[column]}
            for sql, params in connection.calls
        )
