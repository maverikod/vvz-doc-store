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

from .chunk_payload import CLASSIFIER_JOIN_SQL, CLASSIFIER_SELECT_SQL
from .compiler import ExecutionMode, ExecutionPlan, compile_query
from .full_text import FullTextExecutor
from .semantic import _result_from_row as _semantic_result_from_row
from .semantic import _statement as _semantic_statement


RUNTIME_EMBEDDING_PROVIDER = "doc-store-runtime"
RUNTIME_EMBEDDING_MODEL = "doc-store-runtime-2d"
RUNTIME_EMBEDDING_VERSION = "0.1.28"
RUNTIME_EMBEDDING_DIMENSION = 2


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
    "body": "sc.text",
    "text": "sc.text",
    "summary": "sc.block_meta ->> 'summary'",
    "title": "d.title",
}


class RuntimeSearchBoundary:
    """Compile public ChunkQuery requests and execute them against PostgreSQL."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    async def __call__(self, query: ChunkQuery, **_context: Any) -> Any:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
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
            model=RUNTIME_EMBEDDING_MODEL,
            dimension=RUNTIME_EMBEDDING_DIMENSION,
        )
        params["query_vector"] = _vector_literal(vector)
        result = await session.execute(statement, params)
        rows = result.mappings().all()
        return tuple(_semantic_result_from_row(row, index) for index, row in enumerate(rows, 1))

    async def _execute_structured(self, session: Any, plan: ExecutionPlan) -> tuple[SearchResult, ...]:
        params: dict[str, Any] = {}
        predicates = _predicate_sql(plan, params)
        where = ["sc.deleted_at IS NULL", *predicates]
        if plan.limit is not None:
            params["limit"] = plan.limit
        sql = f"""
            SELECT sc.id, sc.document_id, sc.paragraph_id, sc.chapter_id,
                   sc.order_index, sc.text, sc.source_start, sc.source_end,
                   sc.char_count, sc.chunk_type, sc.block_meta,
                   {CLASSIFIER_SELECT_SQL},
                   1.0 AS semantic_score
            FROM semantic_chunks AS sc
            JOIN documents AS d ON d.id = sc.document_id
            {CLASSIFIER_JOIN_SQL}
            WHERE {' AND '.join(where)}
            ORDER BY d.created_at ASC, sc.order_index ASC, sc.id ASC
            {"LIMIT :limit" if plan.limit is not None else ""}
        """
        result = await session.execute(text(sql), params)
        rows = result.mappings().all()
        return tuple(_semantic_result_from_row(row, index) for index, row in enumerate(rows, 1))


def runtime_embedding(text_value: str) -> tuple[float, float]:
    """Return a tiny deterministic smoke-test vector for installed runtime checks."""

    lowered = text_value.lower()
    semantic_terms = ("semantic", "embedding", "embeddings", "vector", "similarity", "hybrid")
    full_text_terms = ("postgresql", "full text", "ranking", "highlight", "index")
    semantic_score = 0.1 + sum(1.0 for term in semantic_terms if term in lowered)
    full_text_score = 0.1 + sum(1.0 for term in full_text_terms if term in lowered)
    total = semantic_score + full_text_score
    return (semantic_score / total, full_text_score / total)


def installed_search_orchestrator(config: Mapping[str, Any] | None = None) -> RuntimeSearchBoundary | None:
    """Create the installed search boundary from env/config, when possible."""

    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return RuntimeSearchBoundary(database_url)


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
    "RUNTIME_EMBEDDING_DIMENSION",
    "RUNTIME_EMBEDDING_MODEL",
    "RUNTIME_EMBEDDING_PROVIDER",
    "RUNTIME_EMBEDDING_VERSION",
    "RuntimeSearchBoundary",
    "installed_search_orchestrator",
    "runtime_embedding",
]
