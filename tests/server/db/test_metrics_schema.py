"""Contract tests for semantic-chunk metrics and feedback persistence."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Iterator
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, create_engine, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Engine

from doc_store_server.db.metrics_schema import (
    FEEDBACK_FIELD_ALIASES,
    METRICS_FIELD_ALIASES,
    SemanticChunkFeedback,
    SemanticChunkMetrics,
)
from doc_store_server.db.schema import metadata


ROOT = Path(__file__).resolve().parents[3]
METRICS_TABLE = "semantic_chunk_metrics"
FEEDBACK_TABLE = "semantic_chunk_feedback"
T001_TABLES = {"documents", "chapters", "paragraphs", "semantic_chunks"}
T002_TABLES = {METRICS_TABLE, FEEDBACK_TABLE}
FORBIDDEN_TABLES = {"tokens", "tags", "links", "embeddings"}


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


def test_metrics_metadata_has_exact_canonical_relations_and_types() -> None:
    assert T002_TABLES <= set(metadata.tables)
    metrics = metadata.tables[METRICS_TABLE]
    feedback = metadata.tables[FEEDBACK_TABLE]
    assert set(metrics.c.keys()) == {
        "chunk_uuid",
        "chunk_version_id",
        "quality_score",
        "coverage",
        "cohesion",
        "boundary_prev",
        "boundary_next",
        "matches",
        "used_in_generation",
        "used_as_input",
        "used_as_context",
    }
    assert set(feedback.c.keys()) == {
        "chunk_uuid",
        "chunk_version_id",
        "accepted",
        "rejected",
        "modifications",
    }
    assert all(isinstance(column.type, UUID) for column in (metrics.c.chunk_uuid, feedback.c.chunk_uuid))
    assert all(metrics.c[name].type.python_type is float for name in {
        "quality_score", "coverage", "cohesion", "boundary_prev", "boundary_next"
    })
    assert metrics.c.matches.type.python_type is int
    assert all(feedback.c[name].type.python_type is int for name in {"accepted", "rejected", "modifications"})
    assert all(metrics.c[name].nullable for name in set(metrics.c.keys()) - {"chunk_uuid"})
    assert all(feedback.c[name].nullable for name in set(feedback.c.keys()) - {"chunk_uuid"})


def test_metrics_and_feedback_are_one_to_one_cascading_children() -> None:
    metrics = metadata.tables[METRICS_TABLE]
    feedback = metadata.tables[FEEDBACK_TABLE]
    assert {column.name for column in metrics.primary_key.columns} == {"chunk_uuid"}
    assert {column.name for column in feedback.primary_key.columns} == {"chunk_uuid"}

    expected = {
        METRICS_TABLE: {
            ("chunk_uuid", "semantic_chunks", "id"),
            ("chunk_version_id", "semantic_chunk_versions", "id"),
        },
        FEEDBACK_TABLE: {
            ("chunk_uuid", METRICS_TABLE, "chunk_uuid"),
            ("chunk_version_id", "semantic_chunk_versions", "id"),
        },
    }
    for table in (metrics, feedback):
        actual = {
            (element.parent.name, element.column.table.name, element.column.name)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            for element in constraint.elements
        }
        assert actual == expected[table.name]
        expected_ondelete = {
            "chunk_uuid": "CASCADE",
            "chunk_version_id": "SET NULL",
        }
        assert {element.parent.name: element.ondelete for element in table.foreign_keys} == expected_ondelete


def test_alias_contract_is_immutable_and_has_no_stored_duplicate_columns() -> None:
    assert isinstance(METRICS_FIELD_ALIASES, MappingProxyType)
    assert dict(METRICS_FIELD_ALIASES) == {
        "quality_score": ("metrics.quality_score", "quality_score"),
        "coverage": ("metrics.coverage", "coverage"),
        "cohesion": ("metrics.cohesion", "cohesion"),
        "boundary_prev": ("metrics.boundary_prev", "boundary_prev"),
        "boundary_next": ("metrics.boundary_next", "boundary_next"),
        "matches": ("metrics.matches", "matches"),
        "used_in_generation": ("metrics.used_in_generation", "used_in_generation"),
        "used_as_input": ("metrics.used_as_input", "used_as_input"),
        "used_as_context": ("metrics.used_as_context", "used_as_context"),
    }
    assert dict(FEEDBACK_FIELD_ALIASES) == {
        "accepted": ("metrics.feedback.accepted", "feedback_accepted"),
        "rejected": ("metrics.feedback.rejected", "feedback_rejected"),
        "modifications": ("metrics.feedback.modifications", "feedback_modifications"),
    }
    with pytest.raises(TypeError):
        METRICS_FIELD_ALIASES["quality_score"] = ("wrong",)  # type: ignore[index]
    assert set(metadata.tables[METRICS_TABLE].c.keys()) == {
        "chunk_uuid",
        "chunk_version_id",
        *METRICS_FIELD_ALIASES,
    }
    assert set(metadata.tables[FEEDBACK_TABLE].c.keys()) == {
        "chunk_uuid",
        "chunk_version_id",
        *FEEDBACK_FIELD_ALIASES,
    }
    assert SemanticChunkMetrics.__table__ is metadata.tables[METRICS_TABLE]
    assert SemanticChunkFeedback.__table__ is metadata.tables[FEEDBACK_TABLE]

    canonical = {
        **{name: index for index, name in enumerate(METRICS_FIELD_ALIASES, 1)},
        **{name: index for index, name in enumerate(FEEDBACK_FIELD_ALIASES, 101)},
    }
    nested = {
        "metrics": {name: canonical[name] for name in METRICS_FIELD_ALIASES},
        "feedback": {name: canonical[name] for name in FEEDBACK_FIELD_ALIASES},
    }
    projected = {
        alias.rsplit(".", 1)[-1]: canonical[name]
        for name, aliases in METRICS_FIELD_ALIASES.items()
        for alias in aliases[1:]
    }
    projected.update(
        {
            alias.rsplit(".", 1)[-1]: canonical[name]
            for name, aliases in FEEDBACK_FIELD_ALIASES.items()
            for alias in aliases[1:]
        }
    )
    assert nested["metrics"]["quality_score"] == projected["quality_score"]
    assert nested["feedback"]["accepted"] == projected["feedback_accepted"]
    assert set(projected) == {
        "quality_score",
        "coverage",
        "cohesion",
        "boundary_prev",
        "boundary_next",
        "matches",
        "used_in_generation",
        "used_as_input",
        "used_as_context",
        "feedback_accepted",
        "feedback_rejected",
        "feedback_modifications",
    }
    assert "metrics" not in metadata.tables["semantic_chunks"].c
    assert not hasattr(SemanticChunkMetrics, "quality_score_alias")
    assert not hasattr(SemanticChunkFeedback, "accepted_alias")


def test_metric_checks_reject_negative_values() -> None:
    checks = _constraint_names(metadata.tables[METRICS_TABLE], CheckConstraint)
    feedback_checks = _constraint_names(metadata.tables[FEEDBACK_TABLE], CheckConstraint)
    assert "ck_semantic_chunk_metrics_semantic_chunk_metrics_matches_nonnegative" in checks
    assert {
        "ck_semantic_chunk_feedback_semantic_chunk_feedback_accepted_nonnegative",
        "ck_semantic_chunk_feedback_semantic_chunk_feedback_rejected_nonnegative",
        "ck_semantic_chunk_feedback_semantic_chunk_feedback_modifications_nonnegative",
    } <= feedback_checks
    assert metadata.tables[METRICS_TABLE].c.matches.type._type_affinity is Integer()._type_affinity


def test_0002_offline_upgrade_and_downgrade_only_manage_t002() -> None:
    migration = _load_migration("0002_chunk_metrics_feedback.py", "metrics_migration")
    upgrade = _offline_sql(migration, "upgrade")
    downgrade = _offline_sql(migration, "downgrade")
    assert {name for name in T002_TABLES if f"CREATE TABLE {name}" in upgrade} == T002_TABLES
    assert {name for name in T002_TABLES if f"DROP TABLE {name}" in downgrade} == T002_TABLES
    assert not any(f"CREATE TABLE {name}" in upgrade for name in T001_TABLES | FORBIDDEN_TABLES)
    assert not any(f"DROP TABLE {name}" in downgrade for name in T001_TABLES | FORBIDDEN_TABLES)
    assert "REFERENCES semantic_chunks (id)" in upgrade
    assert "REFERENCES semantic_chunk_metrics (chunk_uuid)" in upgrade


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


def test_0001_then_0002_round_trip_and_constraints_on_isolated_postgresql(
    postgres_engine: Engine,
) -> None:
    baseline = _load_migration("0001_hierarchy_chunk_root.py", "baseline_migration")
    metrics = _load_migration("0002_chunk_metrics_feedback.py", "metrics_migration_live")
    schema = f"metrics_test_{uuid4().hex}"
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            baseline.upgrade()
            metrics.upgrade()
        names = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        }
        assert names == T001_TABLES | T002_TABLES
        assert not (names & FORBIDDEN_TABLES)

        doc = uuid4()
        chapter = uuid4()
        paragraph = uuid4()
        chunk = uuid4()
        connection.execute(text("INSERT INTO documents (id, source_upload_id, source_version, title, processing_status, processing_attempt, block_meta) VALUES (:id, :id, 1, 't', 'pending', 0, '{}'::jsonb)"), {"id": doc})
        connection.execute(text("INSERT INTO chapters (id, document_id, order_index, level, source_start, source_end, block_meta) VALUES (:id, :doc, 0, 1, 0, 0, '{}'::jsonb)"), {"id": chapter, "doc": doc})
        connection.execute(text("INSERT INTO paragraphs (id, document_id, chapter_id, order_index, text, source_start, source_end, search_weight, block_meta) VALUES (:id, :doc, :chapter, 0, 't', 0, 1, 1, '{}'::jsonb)"), {"id": paragraph, "doc": doc, "chapter": chapter})
        connection.execute(text("INSERT INTO semantic_chunks (id, document_id, paragraph_id, chapter_id, order_index, text, source_start, source_end, char_count, search_weight, block_meta) VALUES (:id, :doc, :paragraph, :chapter, 0, 't', 0, 1, 1, 1, '{}'::jsonb)"), {"id": chunk, "doc": doc, "paragraph": paragraph, "chapter": chapter})
        assert connection.execute(text("SELECT count(*) FROM semantic_chunk_metrics WHERE chunk_uuid = :id"), {"id": chunk}).scalar() == 0
        connection.execute(text("INSERT INTO semantic_chunk_metrics (chunk_uuid, quality_score, matches) VALUES (:id, 0, 0)"), {"id": chunk})
        with pytest.raises(Exception), connection.begin_nested():
            connection.execute(text("INSERT INTO semantic_chunk_metrics (chunk_uuid) VALUES (:id)"), {"id": chunk})
        with pytest.raises(Exception), connection.begin_nested():
            connection.execute(text("INSERT INTO semantic_chunk_feedback (chunk_uuid, accepted) VALUES (:id, -1)"), {"id": chunk})
        connection.execute(text("INSERT INTO semantic_chunk_feedback (chunk_uuid, accepted, rejected, modifications) VALUES (:id, 0, 0, 0)"), {"id": chunk})
        assert connection.execute(text("SELECT accepted, rejected, modifications FROM semantic_chunk_feedback WHERE chunk_uuid = :id"), {"id": chunk}).one() == (0, 0, 0)
        connection.execute(text("DELETE FROM semantic_chunks WHERE id = :id"), {"id": chunk})
        assert connection.execute(text("SELECT count(*) FROM semantic_chunk_metrics WHERE chunk_uuid = :id"), {"id": chunk}).scalar() == 0
        with Operations.context(context):
            metrics.downgrade()
        names_after = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        }
        assert names_after == T001_TABLES
        with Operations.context(context):
            baseline.downgrade()
        connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
