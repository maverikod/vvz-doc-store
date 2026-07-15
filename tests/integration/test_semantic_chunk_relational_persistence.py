"""End-to-end contract scenarios for the relational semantic-chunk aggregate."""

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
from doc_store_server.db.semantic_chunk_repository import SemanticChunkRepository


ROOT = Path(__file__).parents[2]
MIGRATIONS = ROOT / "migrations" / "versions"
MIGRATION_NAMES = (
    "0001_hierarchy_chunk_root",
    "0002_chunk_metrics_feedback",
    "0003_chunk_tokens_tags",
    "0004_chunk_links_embeddings_metadata",
)


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
        "end": updates.pop("end", 14),
        "block_meta": updates.pop(
            "block_meta",
            {
                "chapter_id": str(uuid4()),
                "parent_id": str(uuid4()),
                "markup": "paragraph",
                "unknown": {"nested": {"keep": [1, {"x": True}]}},
            },
        ),
    }
    if metrics is not None:
        payload["metrics"] = metrics
    payload.update(updates)
    return SemanticChunk.from_dict_with_autofill_and_validation(payload)


def _metrics() -> dict[str, Any]:
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


def test_public_mapper_round_trip_preserves_minimal_and_maximal_aggregates() -> None:
    minimal = _chunk(metrics=None, tags=["one"], links=[f"related:{uuid4()}"])
    maximal = _chunk(
        metrics=_metrics(),
        tags=["z", "a"],
        links=[f"parent:{uuid4()}", f"related:{uuid4()}"],
        quality_score=0.8,
        coverage=0.7,
        cohesion=0.6,
        boundary_prev=0.1,
        boundary_next=0.2,
        used_in_generation=True,
        feedback_accepted=3,
        feedback_rejected=1,
        feedback_modifications=2,
        embedding=[1.0, 2.0],
        embedding_model="model-a",
    )

    minimal_rows = to_rows(minimal)
    maximal_rows = to_rows(maximal, embedding_provider="provider", embedding_model_version="v1")
    minimal_expected = minimal.model_copy(
        update={"tags_flat": "one", "link_related": minimal.links[0].split(":", 1)[1]}
    )
    assert from_rows(minimal_rows).model_dump(mode="json") == minimal_expected.model_dump(mode="json")
    restored = from_rows(maximal_rows)
    assert restored.metrics.model_dump(mode="json") == maximal.metrics.model_dump(mode="json")
    assert restored.tags == maximal.tags
    assert restored.links == maximal.links
    assert restored.embedding == [1.0, 2.0]
    assert restored.embedding_model == "model-a"
    assert restored.block_meta == maximal.block_meta


def test_mapper_orders_independent_children_and_derives_aliases_without_duplicate_authority() -> None:
    related, parent = uuid4(), uuid4()
    chunk = _chunk(
        metrics=_metrics(),
        tags=["first", "second"],
        links=[f"parent:{parent}", f"related:{related}"],
        quality_score=0.8,
        feedback_accepted=3,
    )
    rows = to_rows(chunk)
    shuffled = rows.__class__(
        root=rows.root,
        metrics=rows.metrics,
        feedback=rows.feedback,
        tokens=tuple(reversed(rows.tokens)),
        tags=tuple(reversed(rows.tags)),
        links=tuple(reversed(rows.links)),
        embeddings=(
            {"id": uuid4(), "chunk_uuid": UUID(str(chunk.uuid)), "vector": [1.0, 2.0], "model": "m", "dimension": 2, "provider": "p", "model_version": "old", "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc), "active": True},
            {"id": uuid4(), "chunk_uuid": UUID(str(chunk.uuid)), "vector": [3.0, 4.0], "model": "m", "dimension": 2, "provider": "p", "model_version": "new", "created_at": datetime(2024, 2, 1, tzinfo=timezone.utc), "active": True},
        ),
        block_meta=rows.block_meta,
    )
    restored = from_rows(shuffled, requested_model="m", requested_dimension=2)

    assert restored.metrics.tokens == ["canonical-2", "canonical-1"]
    assert restored.metrics.bm25_tokens == ["bm25-1", "bm25-0"]
    assert restored.tags == ["first", "second"]
    assert restored.link_parent == str(parent)
    assert restored.link_related == str(related)
    assert restored.embedding == [3.0, 4.0]
    stored = [rows.root, rows.metrics or {}, rows.feedback or {}, *rows.tokens, *rows.tags, *rows.links, *rows.embeddings, rows.block_meta]
    assert not any({"tags_flat", "link_parent", "link_related", "feedback_accepted"}.intersection(row) for row in stored)
    assert rows.block_meta["promoted"]["parent_id"] == rows.root["block_meta"]["parent_id"]
    assert rows.block_meta["extensions"]["unknown"] == rows.root["block_meta"]["unknown"]


def _database_url() -> str | None:
    url = os.getenv("DOC_STORE_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url or "postgres" not in url:
        return None
    return url.replace("postgresql+psycopg://", "postgresql+asyncpg://").replace("postgresql://", "postgresql+asyncpg://")


def _load_migration(name: str) -> Any:
    path = MIGRATIONS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply_migrations(connection: Any, names: tuple[str, ...] = MIGRATION_NAMES) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    context = MigrationContext.configure(connection)
    with Operations.context(context):
        for name in names:
            _load_migration(name).upgrade()


@pytest.fixture
def database() -> Iterator[tuple[async_sessionmaker[AsyncSession], str]]:
    url = _database_url()
    if url is None:
        pytest.skip("PostgreSQL/pgvector integration requires DOC_STORE_TEST_DATABASE_URL or DATABASE_URL")
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("asyncpg is required for PostgreSQL integration")

    schema = f"semantic_chunk_integration_{uuid4().hex}"
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": f'"{schema}",public'}})

    async def setup() -> None:
        async with engine.begin() as connection:
            try:
                if not await connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")):
                    pytest.skip("PostgreSQL is available but pgvector is not installed")
            except Exception as error:
                pytest.skip(f"PostgreSQL/pgvector unavailable: {error}")
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET search_path TO "{schema}", public'))
            await connection.run_sync(_apply_migrations)

    try:
        asyncio.run(setup())
    except Exception as error:
        asyncio.run(engine.dispose())
        pytest.skip(f"PostgreSQL/pgvector unavailable: {error}")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), schema
    finally:
        async def teardown() -> None:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await engine.dispose()

        asyncio.run(teardown())


async def _seed_hierarchy(session: AsyncSession, chunk: SemanticChunk) -> None:
    rows = to_rows(chunk)
    await session.execute(text("INSERT INTO documents (id, source_upload_id, source_version, title, processing_status, processing_attempt, block_meta) VALUES (:id, :id, 1, 'doc', 'pending', 0, '{}'::jsonb)"), {"id": rows.root["document_id"]})
    await session.execute(text("INSERT INTO chapters (id, document_id, order_index, level, source_start, source_end, block_meta) VALUES (:id, :doc, 0, 1, 0, 14, '{}'::jsonb)"), {"id": rows.root["chapter_id"], "doc": rows.root["document_id"]})
    await session.execute(text("INSERT INTO paragraphs (id, document_id, chapter_id, order_index, text, source_start, source_end, search_weight, block_meta) VALUES (:id, :doc, :chapter, 0, 'semantic body', 0, 14, 1, '{}'::jsonb)"), {"id": rows.root["paragraph_id"], "doc": rows.root["document_id"], "chapter": rows.root["chapter_id"]})


def test_repository_round_trip_ownership_soft_delete_and_full_text_pgvector(database) -> None:
    async def scenario() -> None:
        factory, schema = database
        chunk = _chunk(metrics=_metrics(), tags=["z", "a"], links=[f"related:{uuid4()}"], embedding=[1.0, 2.0], embedding_model="model-a")
        async with factory() as session:
            await _seed_hierarchy(session, chunk)
            await session.commit()
            restored = await SemanticChunkRepository(session).upsert(chunk, embedding_provider="p", embedding_model_version="v1")
            loaded = await SemanticChunkRepository(session).get_by_uuid(UUID(str(chunk.uuid)), requested_model="model-a", requested_dimension=2)
            assert loaded.model_dump(mode="json") == restored.model_dump(mode="json")
            ownership = await session.execute(text("SELECT document_id, chapter_id, paragraph_id, id FROM semantic_chunks WHERE id = :id"), {"id": chunk.uuid})
            assert tuple(ownership.one()) == (UUID(str(chunk.source_id)), UUID(str(chunk.block_meta["chapter_id"])), UUID(str(chunk.block_id)), UUID(str(chunk.uuid)))
            indexes = {row[0] for row in (await session.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema"), {"schema": schema})).all()}
            assert {"ix_paragraphs_search_vector", "ix_semantic_chunks_search_vector", "ix_semantic_chunk_embeddings_vector_cosine"} <= indexes
            await session.execute(text("UPDATE semantic_chunks SET deleted_at = now() WHERE id = :id"), {"id": chunk.uuid})
            assert await session.scalar(text("SELECT deleted_at IS NOT NULL FROM semantic_chunks WHERE id = :id"), {"id": chunk.uuid})

    asyncio.run(scenario())


def test_repository_failure_after_root_and_each_child_family_rolls_back(database, monkeypatch) -> None:
    async def scenario() -> None:
        factory, _ = database
        for method in ("_replace_metrics", "_replace_ordered_children", "_upsert_embeddings"):
            chunk = _chunk(metrics=_metrics(), embedding=[1.0, 2.0], embedding_model="m")
            async with factory() as session:
                await _seed_hierarchy(session, chunk)
                await session.commit()
                repository = SemanticChunkRepository(session)
                original = getattr(repository, method)

                async def fail(*args: Any, _original=original, **kwargs: Any) -> Any:
                    await _original(*args, **kwargs)
                    raise RuntimeError(method)

                monkeypatch.setattr(repository, method, fail)
                with pytest.raises(RuntimeError, match=method):
                    await repository.upsert(chunk)
                await session.rollback()
                assert await session.scalar(text("SELECT count(*) FROM semantic_chunks WHERE id = :id"), {"id": chunk.uuid}) == 0

    asyncio.run(scenario())


def test_stale_child_replacement_and_concurrent_writes_never_mix_aggregates(database) -> None:
    async def scenario() -> None:
        factory, _ = database
        base = _chunk(metrics=_metrics(), embedding=[1.0, 2.0], embedding_model="m")
        first = base.model_copy(update={"body": "first", "text": "first", "tags": ["first"], "metrics": base.metrics.model_copy(update={"tokens": ["first-token"]})})
        second = base.model_copy(update={"body": "second", "text": "second", "tags": ["second"], "metrics": base.metrics.model_copy(update={"tokens": ["second-token"]})})
        async with factory() as session:
            await _seed_hierarchy(session, base)
            await session.commit()
            await SemanticChunkRepository(session).upsert(base)
            await SemanticChunkRepository(session).upsert(first)
            stale = await SemanticChunkRepository(session).get_by_uuid(UUID(str(base.uuid)))
            assert stale.tags == ["first"]
            assert stale.metrics.tokens == ["first-token"]

        async def write(value: SemanticChunk) -> None:
            async with factory() as session:
                await SemanticChunkRepository(session).upsert(value)

        await asyncio.gather(write(first), write(second))
        async with factory() as session:
            loaded = await SemanticChunkRepository(session).get_by_uuid(UUID(str(base.uuid)))
            assert (
                loaded.body == "first"
                and loaded.tags == ["first"]
                and loaded.metrics.tokens == ["first-token"]
            ) or (
                loaded.body == "second"
                and loaded.tags == ["second"]
                and loaded.metrics.tokens == ["second-token"]
            )

    asyncio.run(scenario())


def test_migration_downgrade_upgrade_boundaries_preserve_prior_schema(database) -> None:
    async def scenario() -> None:
        _, schema = database
        url = _database_url()
        assert url is not None
        from sqlalchemy import create_engine

        engine = create_engine(url)
        with engine.begin() as connection:
            connection.exec_driver_sql(f'SET search_path TO "{schema}", public')
            for name in reversed(MIGRATION_NAMES[1:]):
                _load_migration(name).downgrade()
            names = {row[0] for row in connection.exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").all()}
            assert names == {"documents", "chapters", "paragraphs", "semantic_chunks"}
            for name in MIGRATION_NAMES[1:]:
                _load_migration(name).upgrade()
            names = {row[0] for row in connection.exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").all()}
            assert {"semantic_chunk_metrics", "semantic_chunk_feedback", "semantic_chunk_tokens", "semantic_chunk_tags", "semantic_chunk_links", "semantic_chunk_embeddings"} <= names
        engine.dispose()

    asyncio.run(scenario())
