"""Contract tests for chunk links, embeddings, and promoted block metadata."""

from __future__ import annotations

import importlib.util
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Iterator
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from doc_store_server.db.link_embedding_metadata_schema import (
    EMBEDDING_FIELD_ALIASES,
    KNOWN_BLOCK_META_KEYS,
    LINK_COMPATIBILITY_ALIASES,
    LINK_FIELD_ALIASES,
    SemanticChunkEmbedding,
    SemanticChunkLink,
    merge_block_meta,
    promote_block_meta,
    reconstruct_link_ordinals,
    select_active_embedding,
    split_block_meta,
)
from doc_store_server.db.schema import metadata


ROOT = Path(__file__).resolve().parents[3]
ROOT_TABLES = {"documents", "chapters", "paragraphs", "semantic_chunks"}
LINK_TABLE = "semantic_chunk_links"
EMBEDDING_TABLE = "semantic_chunk_embeddings"
T004_TABLES = {LINK_TABLE, EMBEDDING_TABLE}
FORBIDDEN_TABLES = {
    "semantic_chunk_metrics",
    "semantic_chunk_feedback",
    "semantic_chunk_tokens",
    "semantic_chunk_tags",
    "metrics",
    "tokens",
    "tags",
}


def _vector_literal(seed: float) -> str:
    return "[" + ",".join(str(seed + index / 1000) for index in range(384)) + "]"


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


def test_metadata_has_canonical_link_and_versioned_embedding_relations() -> None:
    links = SemanticChunkLink.__table__
    embeddings = SemanticChunkEmbedding.__table__
    assert metadata.tables[LINK_TABLE] is links
    assert metadata.tables[EMBEDDING_TABLE] is embeddings
    assert set(links.c.keys()) == {"source_chunk_uuid", "relation_type", "target_chunk_uuid", "ordinal", "relation_data"}
    assert set(embeddings.c.keys()) == {
        "id", "entity_type", "entity_id", "chunk_uuid", "chunk_version_id", "vector", "model", "dimension",
        "provider", "model_version", "created_at", "active",
    }

    assert isinstance(LINK_FIELD_ALIASES, MappingProxyType)
    assert dict(LINK_FIELD_ALIASES) == {
        "source_chunk_uuid": ("source_chunk_uuid", "links.source_chunk_uuid"),
        "relation_type": ("relation_type", "links.relation_type"),
        "target_chunk_uuid": ("target_chunk_uuid", "links.target_chunk_uuid"),
        "ordinal": ("ordinal", "links.ordinal"),
        "relation_data": ("relation_data", "links.relation_data"),
    }
    assert dict(LINK_COMPATIBILITY_ALIASES) == {
        "links": ("links",), "link_parent": ("link_parent",), "link_related": ("link_related",)
    }
    assert dict(EMBEDDING_FIELD_ALIASES) == {"embedding": ("vector",), "embedding_model": ("model",)}
    assert "links" not in links.c and "link_parent" not in links.c and "embedding_model" not in embeddings.c
    assert not hasattr(SemanticChunkLink, "link_parent")
    assert not hasattr(SemanticChunkEmbedding, "embedding_model_alias")


def test_link_constraints_indexes_and_ordered_queries_are_deterministic() -> None:
    links = metadata.tables[LINK_TABLE]
    assert {
        "uq_semantic_chunk_links_ordered_identity",
        "ck_semantic_chunk_links_semantic_chunk_links_ordinal_nonnegative",
    } <= _constraint_names(links, (UniqueConstraint, CheckConstraint))
    assert set(_index(links, "ix_semantic_chunk_links_source_type").columns) == {
        links.c.source_chunk_uuid, links.c.relation_type
    }
    assert set(_index(links, "ix_semantic_chunk_links_target").columns) == {links.c.target_chunk_uuid}

    source_a, source_b, target = UUID("00000000-0000-0000-0000-000000000001"), UUID("00000000-0000-0000-0000-000000000002"), UUID("00000000-0000-0000-0000-000000000003")
    rows = [
        {"source_chunk_uuid": source_a, "relation_type": "reference", "target_chunk_uuid": target, "ordinal": 1, "relation_data": {"weight": 2}},
        {"source_chunk_uuid": source_a, "relation_type": "reference", "target_chunk_uuid": target, "ordinal": 0, "relation_data": {"weight": 1}},
        {"source_chunk_uuid": source_b, "relation_type": "parent", "target_chunk_uuid": target, "ordinal": 0, "relation_data": {"kind": "tree"}},
    ]
    ordered = reconstruct_link_ordinals(rows)
    assert [(row["source_chunk_uuid"], row["relation_type"], row["target_chunk_uuid"], row["ordinal"]) for row in ordered] == [
        (source_a, "reference", target, 0), (source_a, "reference", target, 1), (source_b, "parent", target, 0)
    ]
    assert ordered[0]["relation_data"] == {"weight": 1}


def test_versioned_embeddings_retain_history_and_select_one_compatible_active_row() -> None:
    embeddings = metadata.tables[EMBEDDING_TABLE]
    assert {
        "uq_semantic_chunk_embeddings_version",
        "uq_semantic_chunk_embeddings_entity_version",
        "ck_semantic_chunk_embeddings_semantic_chunk_embeddings_dimension_positive",
    } <= _constraint_names(
        embeddings, (UniqueConstraint, CheckConstraint)
    )
    assert set(_index(embeddings, "ix_semantic_chunk_embeddings_entity_model").columns) == {
        embeddings.c.entity_type, embeddings.c.entity_id, embeddings.c.model, embeddings.c.dimension
    }
    assert set(_index(embeddings, "ix_semantic_chunk_embeddings_chunk_model").columns) == {
        embeddings.c.chunk_uuid, embeddings.c.model, embeddings.c.dimension
    }
    assert set(_index(embeddings, "ix_semantic_chunk_embeddings_chunk_version_id").columns) == {
        embeddings.c.chunk_version_id,
    }
    vector_index = _index(embeddings, "ix_semantic_chunk_embeddings_vector_cosine")
    assert vector_index.dialect_options["postgresql"]["using"] == "hnsw"
    assert vector_index.dialect_options["postgresql"]["ops"] == {"vector": "vector_cosine_ops"}
    active_index = _index(embeddings, "uq_semantic_chunk_embeddings_active_compatibility")
    assert active_index.unique
    assert active_index.dialect_options["postgresql"]["where"] == "active IS TRUE AND chunk_uuid IS NOT NULL"
    active_entity_index = _index(embeddings, "uq_semantic_chunk_embeddings_active_entity")
    assert active_entity_index.unique
    assert active_entity_index.dialect_options["postgresql"]["where"] == "active IS TRUE"

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {"id": "old", "model": "m", "dimension": 3, "active": True, "created_at": base, "provider": "p", "model_version": "1"},
        {"id": "new", "model": "m", "dimension": 3, "active": True, "created_at": base + timedelta(seconds=1), "provider": "p", "model_version": "2"},
        {"id": "inactive", "model": "m", "dimension": 3, "active": False, "created_at": base + timedelta(seconds=2), "provider": "p", "model_version": "3"},
        {"id": "wrong-dimension", "model": "m", "dimension": 4, "active": True, "created_at": base + timedelta(seconds=3), "provider": "p", "model_version": "4"},
        {"id": "wrong-model", "model": "other", "dimension": 3, "active": True, "created_at": base + timedelta(seconds=4), "provider": "p", "model_version": "5"},
    ]
    assert select_active_embedding(rows, "m", 3) == rows[1]
    assert select_active_embedding(rows, "missing", 3) is None
    embedding = SemanticChunkEmbedding(vector=[1.0], model="m")
    assert embedding.embedding == [1.0]
    assert embedding.embedding_model == "m"


def test_block_meta_split_merge_is_lossless_with_one_authority() -> None:
    value = {
        "parent_id": str(uuid4()), "parent_type": "paragraph", "source_start": 4, "source_end": 9,
        "markup": "**x**", "list_level": 2, "heading_level": 3, "aggregation": {"mode": "sum"},
        "unknown": {"nested": [1, {"keep": True}]}, "future": ["key"],
    }
    parts = split_block_meta(value)
    assert set(parts.promoted) == KNOWN_BLOCK_META_KEYS
    assert parts.extensions == {"unknown": value["unknown"], "future": ["key"]}
    assert promote_block_meta(value) == parts.promoted
    assert merge_block_meta(parts.promoted, parts.extensions) == value
    assert "parent_id" not in parts.extensions and "aggregation" not in parts.extensions


def test_0004_offline_upgrade_and_downgrade_only_manage_t004() -> None:
    migration = _load_migration("0004_chunk_links_embeddings_metadata.py", "link_embedding_migration")
    upgrade = _offline_sql(migration, "upgrade")
    downgrade = _offline_sql(migration, "downgrade")
    assert {table for table in T004_TABLES if f"CREATE TABLE {table}" in upgrade} == T004_TABLES
    assert {table for table in T004_TABLES if f"DROP TABLE {table}" in downgrade} == T004_TABLES
    assert "REFERENCES semantic_chunks (id)" in upgrade
    assert "ON DELETE CASCADE" in upgrade
    assert "CREATE INDEX ix_semantic_chunk_embeddings_vector_cosine" in upgrade
    assert not any(f"CREATE TABLE {table}" in upgrade for table in ROOT_TABLES | FORBIDDEN_TABLES)
    assert not any(f"DROP TABLE {table}" in downgrade for table in ROOT_TABLES | FORBIDDEN_TABLES)
    for column in ("parent_id", "parent_type", "markup", "list_level", "heading_level", "aggregation"):
        assert f'ADD COLUMN {column}' in upgrade
        assert f'DROP COLUMN {column}' in downgrade


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    url = os.getenv("DOC_STORE_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL integration requires DOC_STORE_TEST_DATABASE_URL or DATABASE_URL")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            try:
                if not connection.exec_driver_sql("SELECT 1 FROM pg_extension WHERE extname = 'vector'").scalar():
                    pytest.skip("PostgreSQL is available but pgvector is not installed")
            except Exception as error:
                pytest.skip(f"PostgreSQL/pgvector unavailable: {error}")
        yield engine
    finally:
        engine.dispose()


def test_0001_then_0004_round_trip_constraints_cascades_and_root_preservation(postgres_engine: Engine) -> None:
    baseline = _load_migration("0001_hierarchy_chunk_root.py", "baseline_migration")
    migration = _load_migration("0004_chunk_links_embeddings_metadata.py", "link_embedding_migration_live")
    schema = f"link_embedding_test_{uuid4().hex}"
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.exec_driver_sql(f'SET search_path TO "{schema}", public')
        try:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                baseline.upgrade()
                migration.upgrade()
            names = {row[0] for row in connection.exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
            assert names == ROOT_TABLES | T004_TABLES
            assert not names & FORBIDDEN_TABLES

            doc = chapter = paragraph = chunk = uuid4()
            connection.execute(text("INSERT INTO documents (id, source_upload_id, source_version, title, processing_status, processing_attempt, block_meta) VALUES (:id, :id, 1, 't', 'pending', 0, CAST(:meta AS jsonb))"), {"id": doc, "meta": '{"root":{"keep":true}}'})
            connection.execute(text("INSERT INTO chapters (id, document_id, order_index, level, source_start, source_end, block_meta) VALUES (:id, :doc, 0, 1, 0, 0, '{}'::jsonb)"), {"id": chapter, "doc": doc})
            connection.execute(text("INSERT INTO paragraphs (id, document_id, chapter_id, order_index, text, source_start, source_end, search_weight, block_meta) VALUES (:id, :doc, :chapter, 0, 't', 0, 1, 1, '{}'::jsonb)"), {"id": paragraph, "doc": doc, "chapter": chapter})
            connection.execute(text("INSERT INTO semantic_chunks (id, document_id, paragraph_id, chapter_id, order_index, text, source_start, source_end, char_count, search_weight, block_meta) VALUES (:id, :doc, :paragraph, :chapter, 0, 't', 0, 1, 1, 1, CAST(:meta AS jsonb))"), {"id": chunk, "doc": doc, "paragraph": paragraph, "chapter": chapter, "meta": '{"unknown":{"keep":[1,2]}}'})
            target = uuid4()
            connection.execute(text("INSERT INTO semantic_chunks (id, document_id, paragraph_id, chapter_id, order_index, text, source_start, source_end, char_count, search_weight, block_meta) VALUES (:id, :doc, :paragraph, :chapter, 1, 'target', 0, 1, 1, 1, '{}'::jsonb)"), {"id": target, "doc": doc, "paragraph": paragraph, "chapter": chapter})
            connection.execute(text("INSERT INTO semantic_chunk_links (source_chunk_uuid, relation_type, target_chunk_uuid, ordinal, relation_data) VALUES (:source, 'related', :target, 0, '{\"weight\": 1}'::jsonb)"), {"source": chunk, "target": target})
            with pytest.raises(SQLAlchemyError), connection.begin_nested():
                connection.execute(text("INSERT INTO semantic_chunk_links (source_chunk_uuid, relation_type, target_chunk_uuid, ordinal, relation_data) VALUES (:source, 'related', :target, -1, '{}'::jsonb)"), {"source": chunk, "target": target})
            with pytest.raises(SQLAlchemyError), connection.begin_nested():
                connection.execute(text("INSERT INTO semantic_chunk_embeddings (entity_type, entity_id, chunk_uuid, vector, model, dimension, provider, model_version, active) VALUES ('semantic_chunk', :chunk, :chunk, CAST(:vector AS vector), 'm', 0, 'p', '1', true)"), {"chunk": chunk, "vector": _vector_literal(1.0)})
            connection.execute(text("INSERT INTO semantic_chunk_embeddings (entity_type, entity_id, chunk_uuid, vector, model, dimension, provider, model_version, active) VALUES ('semantic_chunk', :chunk, :chunk, CAST(:vector AS vector), 'm', 384, 'p', '1', false)"), {"chunk": chunk, "vector": _vector_literal(1.0)})
            connection.execute(text("INSERT INTO semantic_chunk_embeddings (entity_type, entity_id, chunk_uuid, vector, model, dimension, provider, model_version, active) VALUES ('semantic_chunk', :chunk, :chunk, CAST(:vector AS vector), 'm', 384, 'p', '2', true)"), {"chunk": chunk, "vector": _vector_literal(4.0)})
            with pytest.raises(SQLAlchemyError), connection.begin_nested():
                connection.execute(text("INSERT INTO semantic_chunk_embeddings (entity_type, entity_id, chunk_uuid, vector, model, dimension, provider, model_version, active) VALUES ('semantic_chunk', :chunk, :chunk, CAST(:vector AS vector), 'm', 384, 'q', '3', true)"), {"chunk": chunk, "vector": _vector_literal(7.0)})
            connection.execute(text("INSERT INTO semantic_chunk_embeddings (entity_type, entity_id, chunk_uuid, vector, model, dimension, provider, model_version, active) VALUES ('document', :document, NULL, CAST(:vector AS vector), 'm', 384, 'p', '1', true)"), {"document": doc, "vector": _vector_literal(9.0)})
            assert connection.execute(text("SELECT count(*) FROM semantic_chunk_embeddings WHERE chunk_uuid = :chunk"), {"chunk": chunk}).scalar() == 2
            assert connection.execute(text("SELECT count(*) FROM semantic_chunk_embeddings WHERE entity_type = 'document' AND entity_id = :document"), {"document": doc}).scalar() == 1
            assert connection.execute(text("SELECT block_meta->'unknown' FROM semantic_chunks WHERE id = :id"), {"id": chunk}).scalar() == {"keep": [1, 2]}
            connection.execute(text("DELETE FROM semantic_chunks WHERE id = :id"), {"id": chunk})
            assert connection.execute(text("SELECT count(*) FROM semantic_chunk_links WHERE source_chunk_uuid = :id"), {"id": chunk}).scalar() == 0
            assert connection.execute(text("SELECT count(*) FROM semantic_chunk_embeddings WHERE chunk_uuid = :id"), {"id": chunk}).scalar() == 0
            with Operations.context(context):
                migration.downgrade()
            names_after = {row[0] for row in connection.exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
            assert names_after == ROOT_TABLES
            with Operations.context(context):
                baseline.downgrade()
        finally:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
