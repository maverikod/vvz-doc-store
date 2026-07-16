"""Contract tests for entity lifecycle registry migration."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations


ROOT = Path(__file__).resolve().parents[3]


def _load(name: str) -> ModuleType:
    path = ROOT / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offline_sql(module: ModuleType, fn: str) -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        getattr(module, fn)()
    return output.getvalue()


def test_0005_adds_boolean_deleted_marker_registry_and_triggers() -> None:
    migration = _load("0005_entity_lifecycle_registry")
    sql = _offline_sql(migration, "upgrade")

    assert "CREATE TABLE entity_uuid_registry" in sql
    for table in ("documents", "chapters", "paragraphs", "semantic_chunks"):
        assert f"ALTER TABLE {table} ADD COLUMN is_deleted" in sql
        assert f"CREATE INDEX ix_{table}_is_deleted" in sql
        assert f"trg_{table}_register_entity_uuid" in sql
        assert f"trg_{table}_unregister_entity_uuid" in sql

    downgrade = _offline_sql(migration, "downgrade")
    assert "DROP TABLE entity_uuid_registry" in downgrade
    for table in ("documents", "chapters", "paragraphs", "semantic_chunks"):
        assert f"DROP INDEX ix_{table}_is_deleted" in downgrade
        assert f"ALTER TABLE {table} DROP COLUMN is_deleted" in downgrade


def test_0007_adds_files_and_owner_links_without_reusing_document_uuid() -> None:
    migration = _load("0007_files_owner_model")
    sql = _offline_sql(migration, "upgrade")

    assert "CREATE TABLE files" in sql
    assert "ALTER TABLE documents ADD COLUMN owner_id" in sql
    assert "ALTER TABLE projects ADD COLUMN owner_id" in sql
    assert "CREATE INDEX ix_files_owner_id" in sql
    assert "CREATE INDEX ix_documents_owner_id" in sql
    assert "trg_files_register_entity_uuid" in sql
    assert "INSERT INTO entity_uuid_registry (entity_table, entity_id)" in sql
    assert "SELECT\n            gen_random_uuid()," in sql
    assert "SET owner_id = f.id" in sql

    downgrade = _offline_sql(migration, "downgrade")
    assert "DROP TABLE files" in downgrade
    assert "ALTER TABLE documents DROP COLUMN owner_id" in downgrade
    assert "ALTER TABLE projects DROP COLUMN owner_id" in downgrade


def test_0006_deduplicates_imported_projects_by_name() -> None:
    migration = _load("0006_projects_table")
    sql = _offline_sql(migration, "upgrade")

    assert "ranked_projects AS" in sql
    assert "PARTITION BY name" in sql
    assert "WHERE row_number = 1" in sql
    assert "ON CONFLICT (name) DO UPDATE" in sql


def test_0008_adds_semantic_chunk_dictionaries_and_references() -> None:
    migration = _load("0008_semantic_chunk_dictionaries")
    sql = _offline_sql(migration, "upgrade")

    for table in ("chunk_types", "chunk_roles", "chunk_statuses", "block_types", "languages", "categories"):
        assert f"CREATE TABLE {table}" in sql
        assert "descr VARCHAR(100) NOT NULL" in sql
        assert f"CONSTRAINT uq_{table}_descr UNIQUE (descr)" in sql
        assert f"CREATE INDEX ix_{table}_lifecycle" in sql
        assert f"trg_{table}_register_entity_uuid" in sql
        assert f"trg_{table}_unregister_entity_uuid" in sql

    for column, table in {
        "chunk_type_id": "chunk_types",
        "role_id": "chunk_roles",
        "status_id": "chunk_statuses",
        "block_type_id": "block_types",
        "language_id": "languages",
        "category_id": "categories",
    }.items():
        assert f"ALTER TABLE semantic_chunks ADD COLUMN {column}" in sql
        assert f"FOREIGN KEY({column}) REFERENCES {table} (id)" in sql
        assert f"CREATE INDEX ix_semantic_chunks_{column}" in sql

    assert "DocBlock" in sql
    assert "UNKNOWN" in sql
    assert "uncategorized" in sql

    downgrade = _offline_sql(migration, "downgrade")
    assert "ALTER TABLE semantic_chunks DROP COLUMN chunk_type_id" in downgrade
    assert "DROP TABLE chunk_types" in downgrade
    assert "DROP TABLE categories" in downgrade


def test_0011_adds_checksum_lifecycle_flags_and_draft_backfill() -> None:
    migration = _load("0011_document_file_checksum_lifecycle")
    sql = _offline_sql(migration, "upgrade")

    assert "ALTER TABLE files ADD COLUMN needs_revectorize" in sql
    assert "ALTER TABLE files ADD COLUMN needs_rechunk" in sql
    assert "ALTER TABLE documents ADD COLUMN checksum_algorithm" in sql
    assert "ALTER TABLE documents ADD COLUMN content_sha256" in sql
    assert "ALTER TABLE documents ADD COLUMN body_sha256" in sql
    assert "ALTER TABLE documents ADD COLUMN needs_revectorize" in sql
    assert "UPDATE documents SET body_sha256 = source_hash" in sql
    assert "UPDATE documents SET processing_status = 'draft' WHERE processing_status = 'completed'" in sql
    assert "CREATE INDEX ix_documents_body_sha256" in sql
    assert "CREATE INDEX ix_files_reprocessing" in sql
    assert "CREATE INDEX ix_documents_reprocessing" in sql

    downgrade = _offline_sql(migration, "downgrade")
    assert "ALTER TABLE documents DROP COLUMN needs_revectorize" in downgrade
    assert "ALTER TABLE files DROP COLUMN needs_rechunk" in downgrade


def test_0013_adds_owner_id_to_all_remaining_entity_tables() -> None:
    migration = _load("0013_owner_id_all_entities")
    sql = _offline_sql(migration, "upgrade")

    for table in (
        "chunk_types",
        "chunk_roles",
        "chunk_statuses",
        "block_types",
        "languages",
        "categories",
        "chapters",
        "paragraphs",
        "semantic_chunks",
    ):
        assert f"ALTER TABLE {table} ADD COLUMN owner_id" in sql
        assert f"CREATE INDEX ix_{table}_owner_id" in sql

    assert "UPDATE chapters SET owner_id = document_id" in sql
    assert "UPDATE paragraphs SET owner_id = chapter_id" in sql
    assert "UPDATE semantic_chunks SET owner_id = paragraph_id" in sql

    downgrade = _offline_sql(migration, "downgrade")
    assert "ALTER TABLE semantic_chunks DROP COLUMN owner_id" in downgrade
    assert "ALTER TABLE chunk_types DROP COLUMN owner_id" in downgrade
