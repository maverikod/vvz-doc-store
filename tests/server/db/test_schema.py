"""Contract tests for the root hierarchy schema and its first migration."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint, create_engine
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.engine import Engine

from doc_store_server.db.schema import metadata


ROOT = Path(__file__).resolve().parents[3]
ROOT_TABLES = {"documents", "chapters", "paragraphs", "semantic_chunks"}
LIFECYCLE_TABLES = {"entity_uuid_registry"}
FORBIDDEN_TABLES = {
    "sentences",
    "asts",
    "metrics",
    "feedback",
    "tokens",
    "tags",
    "links",
    "embeddings",
}


def _load_baseline() -> ModuleType:
    path = ROOT / "migrations" / "versions" / "0001_hierarchy_chunk_root.py"
    spec = importlib.util.spec_from_file_location("doc_store_baseline", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _constraint_names(table: object, constraint_type: type[object]) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, constraint_type)
    }


def _index(table: object, name: str) -> Index:
    indexes = {index.name: index for index in table.indexes}  # type: ignore[attr-defined]
    assert name in indexes
    return indexes[name]


def _offline_baseline_sql() -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        _load_baseline().upgrade()
    return output.getvalue()


def test_metadata_contains_only_root_relations() -> None:
    assert ROOT_TABLES <= set(metadata.tables)
    assert LIFECYCLE_TABLES <= set(metadata.tables)
    assert not (ROOT_TABLES & FORBIDDEN_TABLES)


def test_root_entities_have_uuid_primary_keys_and_owned_foreign_keys() -> None:
    expected_foreign_keys = {
        "documents": set(),
        "chapters": {("document_id", "documents", "id")},
        "paragraphs": {
            ("document_id", "documents", "id"),
            ("chapter_id", "chapters", "id"),
        },
        "semantic_chunks": {
            ("document_id", "documents", "id"),
            ("chapter_id", "chapters", "id"),
            ("paragraph_id", "paragraphs", "id"),
        },
    }
    for table_name in ROOT_TABLES:
        table = metadata.tables[table_name]
        assert all(isinstance(column.type, UUID) for column in table.primary_key.columns)
        actual = {
            (foreign_key.parent.name, foreign_key.column.table.name, foreign_key.column.name)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            for foreign_key in constraint.elements
        }
        assert actual == expected_foreign_keys[table_name]
        assert all(foreign_key.ondelete == "CASCADE" for foreign_key in table.foreign_keys)


def test_ordering_ranges_traceability_and_soft_delete_contract() -> None:
    for table_name in ROOT_TABLES:
        table = metadata.tables[table_name]
        assert {"deleted_at", "is_deleted", "block_meta"} <= set(table.c.keys())
        if table_name != "documents":
            assert "order_index" in table.c
        assert isinstance(table.c.block_meta.type, JSONB)
        assert table.c.block_meta.nullable is False
        assert table.c.deleted_at.nullable is True
        assert table.c.is_deleted.nullable is False
        check_sql = {
            str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert check_sql
        if table_name == "documents":
            assert any("source_version" in expression for expression in check_sql)
        else:
            assert any("order_index" in expression for expression in check_sql)

    assert {"source_upload_id", "source_version", "processing_trace_id"} <= set(
        metadata.tables["documents"].c.keys()
    )
    assert {"source_start", "source_end"} <= set(metadata.tables["chapters"].c.keys())
    assert {"source_start", "source_end"} <= set(metadata.tables["paragraphs"].c.keys())
    assert {"source_start", "source_end"} <= set(metadata.tables["semantic_chunks"].c.keys())
    assert {"processing_status", "processing_attempt", "processing_started_at"} <= set(
        metadata.tables["documents"].c.keys()
    )
    registry = metadata.tables["entity_uuid_registry"]
    assert {"entity_table", "entity_id", "created_at"} <= set(registry.c.keys())
    assert registry.c.entity_id.unique is True or any(
        isinstance(constraint, UniqueConstraint)
        and {column.name for column in constraint.columns} == {"entity_id"}
        for constraint in registry.constraints
    )


def test_typed_lifecycle_scoring_search_and_structural_columns() -> None:
    documents = metadata.tables["documents"]
    paragraphs = metadata.tables["paragraphs"]
    chunks = metadata.tables["semantic_chunks"]
    assert documents.c.processing_status.type.length == 32
    assert paragraphs.c.quality_score.type.python_type is float
    assert chunks.c.score.type.python_type is float
    assert paragraphs.c.search_vector.type._type_affinity is TSVECTOR()._type_affinity
    assert chunks.c.search_vector.type._type_affinity is TSVECTOR()._type_affinity
    assert {"document_id", "chapter_id", "order_index"} <= set(paragraphs.c.keys())
    assert {"document_id", "chapter_id", "paragraph_id", "order_index"} <= set(chunks.c.keys())


def test_uniqueness_checks_and_full_text_indexes_are_named_and_deterministic() -> None:
    expected = {
        "documents": {
            "uq_documents_upload_version",
            "ck_documents_documents_source_version_positive",
        },
        "chapters": {
            "uq_chapters_document_order",
            "ck_chapters_chapters_order_nonnegative",
            "ck_chapters_chapters_source_range_valid",
        },
        "paragraphs": {
            "uq_paragraphs_chapter_order",
            "ck_paragraphs_paragraphs_order_nonnegative",
            "ck_paragraphs_paragraphs_source_range_valid",
        },
        "semantic_chunks": {
            "uq_semantic_chunks_paragraph_order",
            "ck_semantic_chunks_semantic_chunks_order_nonnegative",
            "ck_semantic_chunks_semantic_chunks_source_range_valid",
            "ck_semantic_chunks_semantic_chunks_char_count_nonnegative",
        },
    }
    for table_name, names in expected.items():
        table = metadata.tables[table_name]
        assert names <= _constraint_names(table, (UniqueConstraint, CheckConstraint))

    assert set(_index(metadata.tables["paragraphs"], "ix_paragraphs_search_vector").columns) == {
        metadata.tables["paragraphs"].c.search_vector
    }
    assert set(_index(metadata.tables["semantic_chunks"], "ix_semantic_chunks_search_vector").columns) == {
        metadata.tables["semantic_chunks"].c.search_vector
    }
    assert _index(metadata.tables["paragraphs"], "ix_paragraphs_search_vector").dialect_options[
        "postgresql"
    ]["using"] == "gin"


def test_baseline_offline_upgrade_and_downgrade_cover_exact_root_relations() -> None:
    sql = _offline_baseline_sql()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in sql
    upgrade_tables = {table for table in ROOT_TABLES if f'CREATE TABLE {table}' in sql}
    assert upgrade_tables == ROOT_TABLES
    assert not any(f"CREATE TABLE {table}" in sql for table in FORBIDDEN_TABLES)

    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        _load_baseline().downgrade()
    downgrade_sql = output.getvalue()
    assert {table for table in ROOT_TABLES if f"DROP TABLE {table}" in downgrade_sql} == ROOT_TABLES
    assert not any(f"DROP TABLE {table}" in downgrade_sql for table in FORBIDDEN_TABLES)


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    url = os.getenv("DOC_STORE_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL integration requires DOC_STORE_TEST_DATABASE_URL or DATABASE_URL")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            try:
                available = connection.exec_driver_sql(
                    "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                ).scalar()
                if not available:
                    pytest.skip("PostgreSQL is available but pgvector is not installed")
            except Exception as error:
                pytest.skip(f"PostgreSQL/pgvector unavailable: {error}")
        yield engine
    finally:
        engine.dispose()


def test_baseline_round_trip_on_isolated_postgresql_with_pgvector(postgres_engine: Engine) -> None:
    baseline = _load_baseline()
    schema_name = "doc_store_schema_contract_test"
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')
        connection.exec_driver_sql(f'SET search_path TO "{schema_name}", public')
        try:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                baseline.upgrade()
            names = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                )
            }
            assert ROOT_TABLES <= names
            assert not (FORBIDDEN_TABLES & names)
            with Operations.context(context):
                baseline.downgrade()
            names_after = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                )
            }
            assert not (ROOT_TABLES & names_after)
        finally:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema_name}" CASCADE')
