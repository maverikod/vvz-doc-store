"""Installed runtime search boundary for adapter command execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import os
from typing import Any

from chunk_metadata_adapter import ChunkQuery, SearchResult
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.embedding_config import RuntimeEmbeddingConfig, runtime_embedding_config

from .chunk_payload import (
    CHUNK_TEXT_COLUMN_SQL,
    CHUNK_TEXT_JOIN_SQL,
    CHUNK_TEXT_SELECT_SQL,
    CLASSIFIER_JOIN_SQL,
    CLASSIFIER_SELECT_SQL,
)
from .compiler import ExecutionMode, ExecutionPlan, compile_query
from .full_text import FullTextExecutor
from .semantic import _result_from_row as _semantic_result_from_row
from .semantic import _statement as _semantic_statement


_PREDICATE_COLUMNS = {
    "uuid": "sc.id",
    "document_id": "sc.document_id",
    "paragraph_id": "sc.paragraph_id",
    "chapter_id": "sc.chapter_id",
    "ordinal": "sc.order_index",
    "order_index": "sc.order_index",
    "source_start": "sc.source_start",
    "source_end": "sc.source_end",
    "char_count": "sc.char_count",
    "chunk_type": "sc.chunk_type",
    "score": "sc.score",
    "search_weight": "sc.search_weight",
    "block_meta": "sc.block_meta",
    "project": "sc.block_meta ->> 'project'",
    "source": "sc.block_meta ->> 'source'",
    "source_id": "sc.document_id",
    "block_id": "sc.paragraph_id",
    "body": CHUNK_TEXT_COLUMN_SQL,
    "text": CHUNK_TEXT_COLUMN_SQL,
    "summary": "sc.block_meta ->> 'summary'",
    "title": "d.title",
    "type": "COALESCE(ct.descr, sc.chunk_type, 'DocBlock')",
    "role": "cr.descr",
    "status": "cs.descr",
    "block_type": "bt.descr",
    "language": "lang.descr",
    "category": "cat.descr",
}

_METADATA_FIELDS = {
    "block_id", "block_index", "block_meta", "boundary_next",
    "boundary_prev", "chunking_version", "cohesion", "coverage", "created_at",
    "embedding_model", "end", "feedback_accepted", "feedback_modifications",
    "feedback_rejected", "is_code_chunk", "is_public", "link_parent",
    "link_related", "metrics", "sha256", "source_lines_end",
    "source_lines_start", "source_path", "start", "subtask_id", "tags_flat",
    "task_id", "unit_id", "used_in_generation", "year", "quality_score",
}

for _field in _METADATA_FIELDS:
    _PREDICATE_COLUMNS.setdefault(_field, f"sc.block_meta ->> '{_field}'")


class RuntimeSearchBoundary:
    """Compile public ChunkQuery requests and execute them against PostgreSQL."""

    def __init__(
        self,
        database_url: str | None,
        embedding_config: RuntimeEmbeddingConfig | None = None,
        embedding_client: Any | None = None,
    ) -> None:
        self._database_url = database_url
        self._embedding_config = embedding_config or runtime_embedding_config()
        self._embedding_client = embedding_client

    async def __call__(self, query: ChunkQuery, **_context: Any) -> Any:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        query = await self._query_with_runtime_embedding(query)
        plan = compile_query(query)
        engine = create_async_engine(_async_database_url(self._database_url), pool_pre_ping=True)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                if plan.mode is ExecutionMode.FULL_TEXT:
                    return await FullTextExecutor(session).execute(plan)
                if plan.mode is ExecutionMode.SEMANTIC:
                    return await self._execute_semantic(session, plan)
                if plan.mode is ExecutionMode.STRUCTURED:
                    return await self._execute_structured(session, plan)
                raise RuntimeError("hybrid runtime execution is not configured")
        finally:
            await engine.dispose()

    async def _execute_semantic(self, session: Any, plan: ExecutionPlan) -> tuple[SearchResult, ...]:
        vector = _single_vector(plan.embedding)
        if not plan.order_by:
            plan = replace(plan, order_by=("semantic_score",))
        statement, params = _semantic_statement(
            plan,
            vector,
            model=self._embedding_config.model,
            dimension=self._embedding_config.dimension,
        )
        params["query_vector"] = _vector_literal(vector)
        result = await session.execute(statement, params)
        rows = result.mappings().all()
        return tuple(_semantic_result_from_row(row, index) for index, row in enumerate(rows, 1))

    async def _query_with_runtime_embedding(self, query: ChunkQuery) -> ChunkQuery:
        text_value = query.search_query.strip() if isinstance(query.search_query, str) else ""
        if query.embedding is not None or not text_value:
            return query
        explicit_fields = getattr(query, "model_fields_set", set())
        explicit_semantic_weight = "semantic_weight" in explicit_fields
        wants_runtime_embedding = bool(query.hybrid_search) or (
            explicit_semantic_weight and query.semantic_weight is not None and float(query.semantic_weight) > 0
        )
        if not wants_runtime_embedding:
            return query
        vector = await self._embed_query_text(text_value)
        embedding = list(vector)
        if not query.hybrid_search or float(query.bm25_weight or 0.0) == 0.0:
            return query.model_copy(
                update={
                    "search_query": None,
                    "embedding": embedding,
                    "hybrid_search": False,
                }
            )
        return query.model_copy(update={"embedding": embedding})

    async def _embed_query_text(self, text_value: str) -> tuple[float, ...]:
        client = self._embedding_client
        if client is None:
            from doc_store_server.runtime.vectorization import installed_embedding_client

            client = installed_embedding_client(self._embedding_config)
        response = client.embed(
            [text_value],
            model=self._embedding_config.model,
            dimension=self._embedding_config.dimension,
            wait=True,
            wait_timeout=self._embedding_config.wait_timeout,
            poll_interval=self._embedding_config.poll_interval,
            device=self._embedding_config.device,
        )
        import inspect

        response = await response if inspect.isawaitable(response) else response
        return _embedding_response_vector(response, self._embedding_config.dimension)

    async def _execute_structured(self, session: Any, plan: ExecutionPlan) -> tuple[SearchResult, ...]:
        params: dict[str, Any] = {}
        predicates = _predicate_sql(plan, params)
        where = ["sc.deleted_at IS NULL", *predicates]
        if plan.limit is not None:
            params["limit"] = plan.limit
        if plan.offset:
            params["offset"] = plan.offset
        sql = f"""
            SELECT sc.id, sc.document_id, sc.paragraph_id, sc.chapter_id,
                   sc.order_index, {CHUNK_TEXT_SELECT_SQL}, sc.source_start, sc.source_end,
                   sc.char_count, sc.chunk_type, sc.block_meta,
                   {CLASSIFIER_SELECT_SQL},
                   1.0 AS semantic_score
            FROM semantic_chunks AS sc
            {CHUNK_TEXT_JOIN_SQL}
            JOIN documents AS d ON d.id = sc.document_id
            {CLASSIFIER_JOIN_SQL}
            WHERE {' AND '.join(where)}
            ORDER BY d.created_at ASC, sc.order_index ASC, sc.id ASC
            {"LIMIT :limit" if plan.limit is not None else ""}
            {"OFFSET :offset" if plan.offset else ""}
        """
        result = await session.execute(text(sql), params)
        rows = result.mappings().all()
        return tuple(_semantic_result_from_row(row, index) for index, row in enumerate(rows, 1))


def installed_search_orchestrator(config: Mapping[str, Any] | None = None) -> RuntimeSearchBoundary | None:
    """Create the installed search boundary from env/config, when possible."""

    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return RuntimeSearchBoundary(database_url, runtime_embedding_config(config))


def _async_database_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


def _single_vector(value: Any) -> tuple[float, ...]:
    if not isinstance(value, tuple) or not value:
        raise RuntimeError("semantic search requires query.embedding")
    if isinstance(value[0], tuple):
        return tuple(float(item) for item in value[0])
    return tuple(float(item) for item in value)


def _vector_literal(values: tuple[float, ...]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def _embedding_response_vector(response: Any, dimension: int) -> tuple[float, ...]:
    if not isinstance(response, Mapping):
        raise RuntimeError("embedding client returned a non-mapping response")
    results = response.get("results", response.get("embeddings"))
    if not isinstance(results, list | tuple) or not results:
        raise RuntimeError("embedding response has no results")
    item = results[0]
    if isinstance(item, Mapping):
        error = item.get("error")
        if error:
            raise RuntimeError(f"embedding response item failed: {error}")
        raw = item.get("embedding", item.get("vector"))
    else:
        raw = item
    if not isinstance(raw, list | tuple):
        raise RuntimeError("embedding response vector is not a sequence")
    vector = tuple(float(value) for value in raw)
    if len(vector) != dimension:
        raise RuntimeError("embedding response vector dimension mismatch")
    return vector


def _predicate_sql(plan: ExecutionPlan, params: dict[str, Any]) -> list[str]:
    clauses: list[str] = []
    for predicate in plan.predicates.predicates:
        column = _PREDICATE_COLUMNS.get(predicate.column)
        if column is None and predicate.column in {"tags", "links", "block_meta"}:
            column = f"sc.block_meta -> '{predicate.column}'"
        if column is None:
            raise RuntimeError(f"unsupported compiled predicate: {predicate.column}")
        params[predicate.parameter] = predicate.value
        if predicate.operator == "@>":
            import json

            params[predicate.parameter] = json.dumps(predicate.value)
            clauses.append(f"{column} @> CAST(:{predicate.parameter} AS jsonb)")
        elif predicate.operator == "=":
            clauses.append(f"{column} = :{predicate.parameter}")
        else:
            raise RuntimeError(f"unsupported compiled operator: {predicate.operator}")
    return clauses


__all__ = [
    "RuntimeSearchBoundary",
    "installed_search_orchestrator",
]
