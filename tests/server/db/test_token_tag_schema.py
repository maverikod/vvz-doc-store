"""Contract tests for ordered semantic-chunk token and tag mappings."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
from types import ModuleType
from typing import Iterator
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from doc_store_server.db.schema import metadata
from doc_store_server.db.token_tag_schema import (
    SemanticChunkTag,
    SemanticChunkToken,
    reconstruct_tags,
    reconstruct_tags_flat,
    reconstruct_token_groups,
)


ROOT = Path(__file__).resolve().parents[3]
ROOT_TABLES = {"documents", "chapters", "paragraphs", "semantic_chunks"}
TOKEN_TABLE = "semantic_chunk_tokens"
TAG_TABLE = "semantic_chunk_tags"
T003_TABLES = {TOKEN_TABLE, TAG_TABLE}
FORBIDDEN_TABLES = {
    "semantic_chunk_metrics",
    "semantic_chunk_feedback",
    "semantic_chunk_links",
    "semantic_chunk_embeddings",
    "semantic_chunk_block_meta",
    "metrics",
    "feedback",
    "links",
    "embeddings",
}


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


def test_metadata_has_exact_token_and_tag_columns_and_cascading_ownership() -> None:
    tokens = metadata.tables[TOKEN_TABLE]
    tags = metadata.tables[TAG_TABLE]
    assert set(tokens.c.keys()) == {
        "chunk_uuid",
        "chunk_version_id",
        "token_kind",
        "ordinal",
        "token_value",
    }
    assert set(tags.c.keys()) == {"chunk_uuid", "chunk_version_id", "ordinal", "tag_value"}
    assert set(tokens.primary_key.columns) == {tokens.c.chunk_uuid, tokens.c.token_kind, tokens.c.ordinal}
    assert set(tags.primary_key.columns) == {tags.c.chunk_uuid, tags.c.ordinal}

    expected_foreign_keys = {
        TOKEN_TABLE: {
            ("chunk_uuid", "semantic_chunks", "id"),
            ("chunk_version_id", "semantic_chunk_versions", "id"),
        },
        TAG_TABLE: {
            ("chunk_uuid", "semantic_chunks", "id"),
            ("chunk_version_id", "semantic_chunk_versions", "id"),
        },
    }
    for table in (tokens, tags):
        actual = {
            (element.parent.name, element.column.table.name, element.column.name)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            for element in constraint.elements
        }
        assert actual == expected_foreign_keys[table.name]
        assert {element.parent.name: element.ondelete for element in table.foreign_keys} == {
            "chunk_uuid": "CASCADE",
            "chunk_version_id": "SET NULL",
        }


def test_metadata_enforces_independent_ordered_identity_and_supporting_indexes() -> None:
    tokens = metadata.tables[TOKEN_TABLE]
    tags = metadata.tables[TAG_TABLE]
    assert {
        "uq_semantic_chunk_tokens_identity",
        "ck_semantic_chunk_tokens_semantic_chunk_tokens_kind_valid",
        "ck_semantic_chunk_tokens_semantic_chunk_tokens_ordinal_nonnegative",
    } <= _constraint_names(tokens, (UniqueConstraint, CheckConstraint))
    assert {
        "uq_semantic_chunk_tags_identity",
        "ck_semantic_chunk_tags_semantic_chunk_tags_ordinal_nonnegative",
    } <= _constraint_names(tags, (UniqueConstraint, CheckConstraint))
    assert set(_index(tokens, "ix_semantic_chunk_tokens_chunk_kind_ordinal").columns) == {
        tokens.c.chunk_uuid,
        tokens.c.token_kind,
        tokens.c.ordinal,
    }
    assert set(_index(tokens, "ix_semantic_chunk_tokens_kind_value").columns) == {
        tokens.c.token_kind,
        tokens.c.token_value,
    }
    assert set(_index(tokens, "ix_semantic_chunk_tokens_chunk_version_id").columns) == {
        tokens.c.chunk_version_id,
    }
    assert set(_index(tags, "ix_semantic_chunk_tags_chunk_ordinal").columns) == {
        tags.c.chunk_uuid,
        tags.c.ordinal,
    }
    assert set(_index(tags, "ix_semantic_chunk_tags_chunk_version_id").columns) == {
        tags.c.chunk_version_id,
    }
    assert set(_index(tags, "ix_semantic_chunk_tags_value").columns) == {tags.c.tag_value}


def test_search_fields_remain_on_chunks_and_are_not_replaced_by_tokens_or_tags() -> None:
    chunks = metadata.tables["semantic_chunks"]
    assert {"text", "search_vector"} <= set(chunks.c.keys())
    assert {"text", "search_vector"}.isdisjoint(metadata.tables[TOKEN_TABLE].c.keys())
    assert {"text", "search_vector"}.isdisjoint(metadata.tables[TAG_TABLE].c.keys())
    assert not (T003_TABLES & FORBIDDEN_TABLES)


def test_reconstruction_sorts_interleaved_rows_and_derives_tags_flat() -> None:
    chunk_uuid = uuid4()
    token_rows = [
        SemanticChunkToken(chunk_uuid=chunk_uuid, token_kind="bm25_tokens", ordinal=1, token_value="b1"),
        SemanticChunkToken(chunk_uuid=chunk_uuid, token_kind="tokens", ordinal=2, token_value="t2"),
        SemanticChunkToken(chunk_uuid=chunk_uuid, token_kind="tokens", ordinal=0, token_value="t0"),
        SemanticChunkToken(chunk_uuid=chunk_uuid, token_kind="bm25_tokens", ordinal=0, token_value="b0"),
    ]
    tag_rows = [
        SemanticChunkTag(chunk_uuid=chunk_uuid, ordinal=2, tag_value="third"),
        SemanticChunkTag(chunk_uuid=chunk_uuid, ordinal=0, tag_value="first"),
        SemanticChunkTag(chunk_uuid=chunk_uuid, ordinal=1, tag_value="second"),
    ]
    assert reconstruct_token_groups(token_rows) == {
        "tokens": ("t0", "t2"),
        "bm25_tokens": ("b0", "b1"),
    }
    assert reconstruct_tags(tag_rows) == ("first", "second", "third")
    assert reconstruct_tags_flat(tag_rows) == "first, second, third"
    assert "tags_flat" not in metadata.tables[TAG_TABLE].c.keys()


def test_0003_offline_upgrade_and_downgrade_only_manage_t003() -> None:
    migration = _load_migration("0003_chunk_tokens_tags.py", "token_tag_migration")
    upgrade = _offline_sql(migration, "upgrade")
    downgrade = _offline_sql(migration, "downgrade")
    assert {name for name in T003_TABLES if f"CREATE TABLE {name}" in upgrade} == T003_TABLES
    assert {name for name in T003_TABLES if f"DROP TABLE {name}" in downgrade} == T003_TABLES
    assert not any(f"CREATE TABLE {name}" in upgrade for name in ROOT_TABLES | FORBIDDEN_TABLES)
    assert not any(f"DROP TABLE {name}" in downgrade for name in ROOT_TABLES | FORBIDDEN_TABLES)
    assert "REFERENCES semantic_chunks (id)" in upgrade


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    url = os.getenv("DOC_STORE_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL integration requires DOC_STORE_TEST_DATABASE_URL or DATABASE_URL")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            try:
                if not connection.exec_driver_sql(
                    "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                ).scalar():
                    pytest.skip("PostgreSQL is available but pgvector is not installed")
            except Exception as error:
                pytest.skip(f"PostgreSQL/pgvector unavailable: {error}")
        yield engine
    finally:
        engine.dispose()


def test_0001_then_0003_round_trip_constraints_and_root_preservation(
    postgres_engine: Engine,
) -> None:
    baseline = _load_migration("0001_hierarchy_chunk_root.py", "baseline_migration")
    migration = _load_migration("0003_chunk_tokens_tags.py", "token_tag_migration_live")
    schema = f"token_tag_test_{uuid4().hex}"
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            baseline.upgrade()
            migration.upgrade()
        names = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        }
        assert names == ROOT_TABLES | T003_TABLES

        doc = uuid4()
        chapter = uuid4()
        paragraph = uuid4()
        chunk = uuid4()
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, source_upload_id, source_version, title, processing_status, processing_attempt, block_meta) "
                "VALUES (:id, :id, 1, 'doc', 'pending', 0, '{}'::jsonb)"
            ),
            {"id": doc},
        )
        connection.execute(
            text(
                "INSERT INTO chapters "
                "(id, document_id, order_index, level, source_start, source_end, block_meta) "
                "VALUES (:id, :doc, 0, 1, 0, 10, '{}'::jsonb)"
            ),
            {"id": chapter, "doc": doc},
        )
        connection.execute(
            text(
                "INSERT INTO paragraphs "
                "(id, document_id, chapter_id, order_index, text, source_start, source_end, search_weight, block_meta) "
                "VALUES (:id, :doc, :chapter, 0, 'searchable', 0, 10, 1, '{}'::jsonb)"
            ),
            {"id": paragraph, "doc": doc, "chapter": chapter},
        )
        connection.execute(
            text(
                "INSERT INTO semantic_chunks "
                "(id, document_id, paragraph_id, chapter_id, order_index, text, "
                "source_start, source_end, char_count, search_weight, block_meta) "
                "VALUES (:id, :doc, :paragraph, :chapter, 0, 'searchable', 0, 10, 10, 1, '{}'::jsonb)"
            ),
            {"id": chunk, "doc": doc, "paragraph": paragraph, "chapter": chapter},
        )
        connection.execute(
            text("INSERT INTO semantic_chunk_tokens (chunk_uuid, token_kind, ordinal, token_value) VALUES (:id, 'tokens', 0, 'one')"),
            {"id": chunk},
        )
        connection.execute(
            text("INSERT INTO semantic_chunk_tokens (chunk_uuid, token_kind, ordinal, token_value) VALUES (:id, 'bm25_tokens', 0, 'one')"),
            {"id": chunk},
        )
        connection.execute(
            text("INSERT INTO semantic_chunk_tags (chunk_uuid, ordinal, tag_value) VALUES (:id, 0, 'tag')"), {"id": chunk}
        )
        for statement in (
            "INSERT INTO semantic_chunk_tokens (chunk_uuid, token_kind, ordinal, token_value) VALUES (:id, 'tokens', -1, 'bad')",
            "INSERT INTO semantic_chunk_tags (chunk_uuid, ordinal, tag_value) VALUES (:id, -1, 'bad')",
            "INSERT INTO semantic_chunk_tokens (chunk_uuid, token_kind, ordinal, token_value) VALUES (:id, 'tokens', 0, 'duplicate')",
            "INSERT INTO semantic_chunk_tags (chunk_uuid, ordinal, tag_value) VALUES (:id, 0, 'duplicate')",
        ):
            with pytest.raises(SQLAlchemyError), connection.begin_nested():
                connection.execute(text(statement), {"id": chunk})

        with Operations.context(context):
            migration.downgrade()
        names_after = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        }
        assert names_after == ROOT_TABLES
        with Operations.context(context):
            baseline.downgrade()
        connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
