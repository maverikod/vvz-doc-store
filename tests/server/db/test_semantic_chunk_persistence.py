"""Mapper and repository contract tests for the semantic-chunk aggregate."""

from __future__ import annotations

import asyncio
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

import pytest
from chunk_metadata_adapter import SemanticChunk
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from doc_store_server.db.semantic_chunk_mapper import from_rows, to_rows
from doc_store_server.db.semantic_chunk_repository import (
    SemanticChunkNotFoundError,
    SemanticChunkRepository,
)


ROOT = Path(__file__).parents[3]
MIGRATIONS = ROOT / "migrations" / "versions"


def _chunk(*, metrics: dict[str, Any] | None = None, **updates: Any) -> SemanticChunk:
    document_id = updates.pop("source_id", uuid4())
    paragraph_id = updates.pop("block_id", uuid4())
    payload: dict[str, Any] = {
        "uuid": str(updates.pop("uuid", uuid4())),
        "source_id": str(document_id),
        "block_id": str(paragraph_id),
        "type": "DocBlock",
        "body": updates.pop("body", "semantic body"),
        "text": updates.pop("text", "semantic body"),
        "ordinal": updates.pop("ordinal", 0),
        "start": updates.pop("start", 0),
        "end": updates.pop("end", 13),
        "block_meta": updates.pop(
            "block_meta", {"chapter_id": str(uuid4()), "parent_id": str(uuid4()), "unknown": {"keep": [1, 2]}}
        ),
    }
    if metrics is not None:
        payload["metrics"] = metrics
    payload.update(updates)
    return SemanticChunk.from_dict_with_autofill_and_validation(payload)


def _rich_metrics() -> dict[str, Any]:
    return {
        "quality_score": 0.8,
        "coverage": 0.7,
        "cohesion": 0.6,
        "boundary_prev": 0.1,
        "boundary_next": 0.2,
        "matches": 4,
        "used_in_generation": True,
        "used_as_input": True,
        "used_as_context": False,
        "feedback": {"accepted": 3, "rejected": 1, "modifications": 2},
        "tokens": ["canonical-2", "canonical-1"],
        "bm25_tokens": ["bm25-1", "bm25-0"],
    }


def test_mapper_round_trips_absent_metrics_through_public_adapter_contract() -> None:
    chunk = _chunk(metrics=None, tags=["one"], links=[f"related:{uuid4()}"])
    rows = to_rows(chunk)
    restored = from_rows(rows)

    assert restored.uuid == chunk.uuid
    assert restored.body == chunk.body
    assert restored.metrics is None
    assert restored.tags == ["one"]
    assert restored.tags_flat == "one"
    assert restored.links == chunk.links


def test_mapper_round_trips_metrics_feedback_tokens_and_all_derived_aliases() -> None:
    related = uuid4()
    parent = uuid4()
    chunk = _chunk(
        metrics=_rich_metrics(),
        tags=["z", "a"],
        links=[f"parent:{parent}", f"related:{related}"],
        quality_score=0.8,
        coverage=0.7,
        cohesion=0.6,
        boundary_prev=0.1,
        boundary_next=0.2,
        used_in_generation=True,
        feedback_accepted=3,
        feedback_rejected=1,
        feedback_modifications=2,
    )
    rows = to_rows(chunk)
    restored = from_rows(rows)

    assert restored.metrics.model_dump(mode="json") == chunk.metrics.model_dump(mode="json")
    assert restored.quality_score == restored.metrics.quality_score == 0.8
    assert restored.coverage == restored.metrics.coverage == 0.7
    assert restored.cohesion == restored.metrics.cohesion == 0.6
    assert restored.boundary_prev == restored.metrics.boundary_prev == 0.1
    assert restored.boundary_next == restored.metrics.boundary_next == 0.2
    assert restored.used_in_generation is restored.metrics.used_in_generation
    assert restored.feedback_accepted == restored.metrics.feedback.accepted == 3
    assert restored.feedback_rejected == restored.metrics.feedback.rejected == 1
    assert restored.feedback_modifications == restored.metrics.feedback.modifications == 2
    assert restored.tags == ["z", "a"]
    assert restored.tags_flat == "z, a"
    assert restored.link_parent == str(parent)
    assert restored.link_related == str(related)
    assert restored.links == chunk.links
    assert restored.metrics.tokens == ["canonical-2", "canonical-1"]
    assert restored.metrics.bm25_tokens == ["bm25-1", "bm25-0"]


def test_mapper_orders_independent_children_and_selects_latest_compatible_embedding() -> None:
    chunk = _chunk(metrics=_rich_metrics(), tags=["first", "second"])
    rows = to_rows(chunk)
    chunk_id = UUID(str(chunk.uuid))
    rows = rows.__class__(
        root=rows.root,
        metrics=rows.metrics,
        feedback=rows.feedback,
        tokens=tuple(reversed(rows.tokens)),
        tags=tuple(reversed(rows.tags)),
        links=rows.links,
        embeddings=(
            {"id": uuid4(), "chunk_uuid": chunk_id, "vector": [1.0, 2.0], "model": "m", "dimension": 2, "provider": "p", "model_version": "old", "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc), "active": True},
            {"id": uuid4(), "chunk_uuid": chunk_id, "vector": [3.0, 4.0], "model": "m", "dimension": 2, "provider": "p", "model_version": "new", "created_at": datetime(2024, 2, 1, tzinfo=timezone.utc), "active": True},
            {"id": uuid4(), "chunk_uuid": chunk_id, "vector": [9.0, 9.0], "model": "other", "dimension": 2, "provider": "p", "model_version": "x", "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc), "active": True},
        ),
        block_meta=rows.block_meta,
    )

    restored = from_rows(rows, requested_model="m", requested_dimension=2)
    assert restored.metrics.tokens == ["canonical-2", "canonical-1"]
    assert restored.metrics.bm25_tokens == ["bm25-1", "bm25-0"]
    assert restored.tags == ["first", "second"]
    assert restored.embedding == [3.0, 4.0]
    assert restored.embedding_model == "m"


def test_decomposed_rows_have_one_canonical_value_per_stored_field() -> None:
    rows = to_rows(
        _chunk(
            metrics=_rich_metrics(),
            tags=["one"],
            links=[f"related:{uuid4()}"],
            quality_score=0.8,
            feedback_accepted=3,
        )
    )
    stored = [rows.root, rows.metrics or {}, rows.feedback or {}, *rows.tokens, *rows.tags, *rows.links, *rows.embeddings, rows.block_meta]
    forbidden = {"tags_flat", "link_parent", "link_related", "feedback_accepted", "feedback_rejected", "feedback_modifications"}
    assert not any(forbidden.intersection(row) for row in stored)
    assert set(rows.root["block_meta"]) == {"chapter_id", "parent_id", "unknown"}
    assert rows.block_meta["promoted"]["parent_id"] == rows.root["block_meta"]["parent_id"]
    assert rows.block_meta["extensions"]["unknown"] == {"keep": [1, 2]}


def test_mapper_rejects_mismatched_compatibility_alias() -> None:
    with pytest.raises(ValueError, match="conflicting compatibility alias"):
        to_rows(_chunk(metrics=_rich_metrics(), quality_score=0.2))


def _async_url() -> str | None:
    url = os.getenv("DOC_STORE_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url or "postgres" not in url:
        return None
    return url.replace("postgresql+psycopg://", "postgresql+asyncpg://").replace("postgresql://", "postgresql+asyncpg://")


@pytest.fixture
def db() -> Iterator[tuple[async_sessionmaker[AsyncSession], Any]]:
    url = _async_url()
    if url is None:
        pytest.skip("PostgreSQL integration requires DOC_STORE_TEST_DATABASE_URL or DATABASE_URL")
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("asyncpg is required for repository integration")
    schema = f"semantic_chunk_test_{uuid4().hex}"
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": f'"{schema}",public'}})

    async def setup() -> None:
        async with engine.begin() as connection:
            try:
                await connection.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            except Exception as error:
                pytest.skip(f"PostgreSQL/pgvector unavailable: {error}")
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET search_path TO "{schema}", public'))
            await connection.run_sync(_upgrade_all)

    asyncio.run(setup())
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), schema
    finally:
        async def teardown() -> None:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await engine.dispose()

        asyncio.run(teardown())


def _upgrade_all(connection: Any) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    context = MigrationContext.configure(connection)
    with Operations.context(context):
        for name in ("0001_hierarchy_chunk_root", "0002_chunk_metrics_feedback", "0003_chunk_tokens_tags", "0004_chunk_links_embeddings_metadata"):
            path = MIGRATIONS / f"{name}.py"
            spec = importlib.util.spec_from_file_location(name, path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.upgrade()


async def _seed_parent(session: AsyncSession, chunk: SemanticChunk) -> None:
    rows = to_rows(chunk)
    await session.execute(text("INSERT INTO documents (id, source_upload_id, source_version, title, processing_status, processing_attempt, block_meta) VALUES (:id, :id, 1, 'doc', 'pending', 0, '{}'::jsonb)"), {"id": rows.root["document_id"]})
    await session.execute(text("INSERT INTO chapters (id, document_id, order_index, level, source_start, source_end, block_meta) VALUES (:id, :doc, 0, 1, 0, 13, '{}'::jsonb)"), {"id": rows.root["chapter_id"], "doc": rows.root["document_id"]})
    await session.execute(text("INSERT INTO paragraphs (id, document_id, chapter_id, order_index, text, source_start, source_end, search_weight, block_meta) VALUES (:id, :doc, :chapter, 0, 'semantic body', 0, 13, 1, '{}'::jsonb)"), {"id": rows.root["paragraph_id"], "doc": rows.root["document_id"], "chapter": rows.root["chapter_id"]})


def test_repository_upsert_read_update_not_found_and_history(db) -> None:
    async def scenario() -> None:
        factory, _ = db
        chunk = _chunk(metrics=_rich_metrics(), embedding=[1.0, 2.0], embedding_model="m")
        async with factory() as session:
            await _seed_parent(session, chunk)
            await session.commit()
            repository = SemanticChunkRepository(session)
            first = await repository.upsert(chunk, embedding_provider="p", embedding_model_version="1", embedding_created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
            updated = chunk.model_copy(update={"body": "updated body", "text": "updated body", "end": 12})
            await repository.upsert(updated, embedding_provider="p", embedding_model_version="2", embedding_created_at=datetime(2024, 2, 1, tzinfo=timezone.utc))
            restored = await repository.get_by_uuid(UUID(str(chunk.uuid)), requested_model="m", requested_dimension=2)
            assert first.uuid == restored.uuid == chunk.uuid
            assert restored.body == "updated body"
            assert restored.embedding == [1.0, 2.0]
            with pytest.raises(SemanticChunkNotFoundError):
                await repository.get_by_uuid(uuid4())

    asyncio.run(scenario())


def test_repository_rolls_back_after_root_and_each_child_failure(db, monkeypatch) -> None:
    async def scenario() -> None:
        factory, _ = db
        for method in ("_replace_metrics", "_replace_ordered_children", "_upsert_embeddings"):
            chunk = _chunk(metrics=_rich_metrics(), embedding=[1.0, 2.0], embedding_model="m")
            async with factory() as session:
                await _seed_parent(session, chunk)
                await session.commit()
                repository = SemanticChunkRepository(session)
                original = getattr(repository, method)

                async def fail(*args: Any, _original=original, **kwargs: Any) -> Any:
                    await _original(*args, **kwargs)
                    raise RuntimeError(method)

                monkeypatch.setattr(repository, method, fail)
                with pytest.raises(RuntimeError):
                    await repository.upsert(chunk)
                await session.rollback()
                assert (await session.execute(text("SELECT count(*) FROM semantic_chunks WHERE id = :id"), {"id": chunk.uuid})).scalar_one() == 0

    asyncio.run(scenario())


def test_repository_concurrent_same_chunk_keeps_one_complete_state(db) -> None:
    async def scenario() -> None:
        factory, _ = db
        chunk = _chunk(metrics=_rich_metrics())
        first = chunk.model_copy(update={"body": "first", "text": "first"})
        second = chunk.model_copy(update={"body": "second", "text": "second"})
        async with factory() as session:
            await _seed_parent(session, chunk)
            await session.commit()

        async def write(value: SemanticChunk) -> None:
            async with factory() as session:
                await SemanticChunkRepository(session).upsert(value)

        await asyncio.gather(write(first), write(second))
        async with factory() as session:
            restored = await SemanticChunkRepository(session).get_by_uuid(UUID(str(chunk.uuid)))
            assert restored.body in {"first", "second"}
            assert restored.metrics.tokens == ["canonical-2", "canonical-1"]
            assert restored.metrics.bm25_tokens == ["bm25-1", "bm25-0"]

    asyncio.run(scenario())
