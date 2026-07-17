"""Installed runtime search boundary for adapter command execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
import os
from typing import Any

from chunk_metadata_adapter import ChunkQuery, SearchResult
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.embedding_config import RuntimeEmbeddingConfig, runtime_embedding_config
from doc_store_server.runtime.search_config import (
    RuntimeSearchConfig,
    RuntimeSemanticRefinementConfig,
    runtime_search_config,
)

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
        search_config: RuntimeSearchConfig | None = None,
        embedding_client: Any | None = None,
    ) -> None:
        self._database_url = database_url
        self._embedding_config = embedding_config or runtime_embedding_config()
        self._search_config = search_config or runtime_search_config()
        self._embedding_client = embedding_client

    async def __call__(self, query: ChunkQuery, **_context: Any) -> Any:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        refinement = _semantic_refinement_options(
            _context.get("semantic_refinement"),
            self._search_config.semantic_refinement,
        )
        query = await self._query_with_runtime_embedding(query)
        plan = compile_query(query)
        engine = create_async_engine(_async_database_url(self._database_url), pool_pre_ping=True)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                if plan.mode is ExecutionMode.FULL_TEXT:
                    return await FullTextExecutor(session).execute(plan)
                if plan.mode is ExecutionMode.SEMANTIC:
                    return await self._execute_semantic(session, plan, refinement=refinement)
                if plan.mode is ExecutionMode.STRUCTURED:
                    return await self._execute_structured(session, plan)
                raise RuntimeError("hybrid runtime execution is not configured")
        finally:
            await engine.dispose()

    async def _execute_semantic(
        self,
        session: Any,
        plan: ExecutionPlan,
        *,
        refinement: Mapping[str, Any],
    ) -> Any:
        vector = _single_vector(plan.embedding)
        if refinement["enabled"]:
            return await self._execute_hierarchical_semantic(session, vector, refinement)
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

    async def _execute_hierarchical_semantic(
        self,
        session: Any,
        vector: tuple[float, ...],
        refinement: Mapping[str, Any],
    ) -> dict[str, Any]:
        vector_value = _vector_literal(vector)
        threshold = float(refinement["threshold"])
        candidate_limit = int(refinement["candidate_limit"])
        result_limit = int(refinement["result_limit"])
        diagnostics_enabled = bool(refinement["diagnostics"])
        primary = await _hierarchical_primary_candidates(
            session,
            vector_value=vector_value,
            model=self._embedding_config.model,
            dimension=self._embedding_config.dimension,
            threshold=threshold,
            limit=candidate_limit,
        )
        window: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        evicted_total = 0
        for candidate in primary:
            before = [item["key"] for item in window]
            if candidate["level"] == "semantic_chunk":
                _merge_window(window, [candidate], result_limit)
            elif candidate["level"] == "paragraph":
                children = await _hierarchical_children(
                    session,
                    vector_value=vector_value,
                    model=self._embedding_config.model,
                    dimension=self._embedding_config.dimension,
                    threshold=threshold,
                    limit=result_limit,
                    paragraph_id=candidate["id"],
                )
                _merge_window(window, children or [candidate], result_limit)
            elif candidate["level"] == "document":
                children, path = await _refine_document_candidate(
                    session,
                    vector_value=vector_value,
                    model=self._embedding_config.model,
                    dimension=self._embedding_config.dimension,
                    threshold=threshold,
                    limit=result_limit,
                    document_id=candidate["id"],
                )
                _merge_window(window, children or [candidate], result_limit)
                candidate["refinement_path"] = path
            elif candidate["level"] == "file":
                children, path = await _refine_file_candidate(
                    session,
                    vector_value=vector_value,
                    model=self._embedding_config.model,
                    dimension=self._embedding_config.dimension,
                    threshold=threshold,
                    limit=result_limit,
                    file_id=candidate["id"],
                )
                _merge_window(window, children or [candidate], result_limit)
                candidate["refinement_path"] = path
            after = [item["key"] for item in window]
            evicted = max(0, len(set(before) - set(after)))
            evicted_total += evicted
            if diagnostics_enabled:
                diagnostics.append(
                    {
                        "candidate": {"id": candidate["id"], "level": candidate["level"], "score": candidate["score"]},
                        "window_size": len(window),
                        "evicted_tail_count": evicted,
                        "refinement_path": candidate.get("refinement_path", []),
                    }
                )
            if len(window) >= result_limit and all(item["score"] >= primary[-1]["score"] for item in window):
                continue
        return {
            "status": "success",
            "data": {
                "results": [
                    {key: value for key, value in item.items() if key != "key"}
                    for item in window[:result_limit]
                ],
                "semantic_refinement": {
                    "threshold": threshold,
                    "candidate_limit": candidate_limit,
                    "result_limit": result_limit,
                    "primary_candidate_count": len(primary),
                    "evicted_tail_count": evicted_total,
                    "model": self._embedding_config.model,
                    "dimension": self._embedding_config.dimension,
                    **({"diagnostics": diagnostics} if diagnostics_enabled else {}),
                },
            },
        }

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
    return RuntimeSearchBoundary(
        database_url,
        runtime_embedding_config(config),
        runtime_search_config(config),
    )


async def _hierarchical_primary_candidates(
    session: Any,
    *,
    vector_value: str,
    model: str,
    dimension: int,
    threshold: float,
    limit: int,
) -> list[dict[str, Any]]:
    sql = """
        WITH candidates AS (
            SELECT 'semantic_chunk'::text AS level, sc.id, sc.document_id, d.owner_id AS file_id,
                   sc.paragraph_id, sc.id AS chunk_id, left(sct.text, 240) AS preview,
                   1.0 - (sce.vector <=> CAST(:query_vector AS vector)) AS score
            FROM semantic_chunk_embeddings AS sce
            JOIN semantic_chunks AS sc ON sc.id = sce.entity_id
            JOIN semantic_chunk_texts AS sct ON sct.chunk_uuid = sc.id
            JOIN documents AS d ON d.id = sc.document_id
            WHERE sce.entity_type = 'semantic_chunk' AND sce.active IS TRUE
              AND sce.model = :model AND sce.dimension = :dimension
              AND sc.deleted_at IS NULL
            UNION ALL
            SELECT 'paragraph'::text AS level, p.id, p.document_id, d.owner_id AS file_id,
                   p.id AS paragraph_id, NULL::uuid AS chunk_id, left(p.text, 240) AS preview,
                   1.0 - (sce.vector <=> CAST(:query_vector AS vector)) AS score
            FROM semantic_chunk_embeddings AS sce
            JOIN paragraphs AS p ON p.id = sce.entity_id
            JOIN documents AS d ON d.id = p.document_id
            WHERE sce.entity_type = 'paragraph' AND sce.active IS TRUE
              AND sce.model = :model AND sce.dimension = :dimension
              AND p.deleted_at IS NULL
            UNION ALL
            SELECT 'document'::text AS level, d.id, d.id AS document_id, d.owner_id AS file_id,
                   NULL::uuid AS paragraph_id, NULL::uuid AS chunk_id, left(d.title, 240) AS preview,
                   1.0 - (sce.vector <=> CAST(:query_vector AS vector)) AS score
            FROM semantic_chunk_embeddings AS sce
            JOIN documents AS d ON d.id = sce.entity_id
            WHERE sce.entity_type = 'document' AND sce.active IS TRUE
              AND sce.model = :model AND sce.dimension = :dimension
              AND d.deleted_at IS NULL
            UNION ALL
            SELECT 'file'::text AS level, f.id, d.id AS document_id, f.id AS file_id,
                   NULL::uuid AS paragraph_id, NULL::uuid AS chunk_id,
                   left(coalesce(f.name, f.path, f.id::text), 240) AS preview,
                   1.0 - (sce.vector <=> CAST(:query_vector AS vector)) AS score
            FROM semantic_chunk_embeddings AS sce
            JOIN files AS f ON f.id = sce.entity_id
            LEFT JOIN LATERAL (
                SELECT id FROM documents
                WHERE owner_id = f.id AND deleted_at IS NULL
                ORDER BY created_at ASC, id ASC
                LIMIT 1
            ) AS d ON TRUE
            WHERE sce.entity_type = 'file' AND sce.active IS TRUE
              AND sce.model = :model AND sce.dimension = :dimension
              AND f.deleted_at IS NULL
        )
        SELECT * FROM candidates
        WHERE score >= :threshold
        ORDER BY score DESC, level ASC, id ASC
        LIMIT :limit
    """
    result = await session.execute(
        text(sql),
        {
            "query_vector": vector_value,
            "model": model,
            "dimension": dimension,
            "threshold": threshold,
            "limit": limit,
        },
    )
    return [_candidate_from_row(row) for row in result.mappings().all()]


async def _hierarchical_children(
    session: Any,
    *,
    vector_value: str,
    model: str,
    dimension: int,
    threshold: float,
    limit: int,
    document_id: str | None = None,
    file_id: str | None = None,
    paragraph_id: str | None = None,
) -> list[dict[str, Any]]:
    scope = []
    params: dict[str, Any] = {
        "query_vector": vector_value,
        "model": model,
        "dimension": dimension,
        "threshold": threshold,
        "limit": limit,
    }
    if document_id is not None:
        scope.append("document_id = :document_id")
        params["document_id"] = document_id
    if file_id is not None:
        scope.append("file_id = :file_id")
        params["file_id"] = file_id
    if paragraph_id is not None:
        scope.append("paragraph_id = :paragraph_id")
        params["paragraph_id"] = paragraph_id
    if not scope:
        raise RuntimeError("hierarchical child refinement requires a scope")
    sql = f"""
        WITH candidates AS (
            SELECT 'semantic_chunk'::text AS level, sc.id, sc.document_id, d.owner_id AS file_id,
                   sc.paragraph_id, sc.id AS chunk_id, left(sct.text, 240) AS preview,
                   1.0 - (sce.vector <=> CAST(:query_vector AS vector)) AS score
            FROM semantic_chunk_embeddings AS sce
            JOIN semantic_chunks AS sc ON sc.id = sce.entity_id
            JOIN semantic_chunk_texts AS sct ON sct.chunk_uuid = sc.id
            JOIN documents AS d ON d.id = sc.document_id
            WHERE sce.entity_type = 'semantic_chunk' AND sce.active IS TRUE
              AND sce.model = :model AND sce.dimension = :dimension
              AND sc.deleted_at IS NULL
            UNION ALL
            SELECT 'paragraph'::text AS level, p.id, p.document_id, d.owner_id AS file_id,
                   p.id AS paragraph_id, NULL::uuid AS chunk_id, left(p.text, 240) AS preview,
                   1.0 - (sce.vector <=> CAST(:query_vector AS vector)) AS score
            FROM semantic_chunk_embeddings AS sce
            JOIN paragraphs AS p ON p.id = sce.entity_id
            JOIN documents AS d ON d.id = p.document_id
            WHERE sce.entity_type = 'paragraph' AND sce.active IS TRUE
              AND sce.model = :model AND sce.dimension = :dimension
              AND p.deleted_at IS NULL
        )
        SELECT * FROM candidates
        WHERE score >= :threshold AND {' AND '.join(scope)}
        ORDER BY score DESC, CASE level WHEN 'semantic_chunk' THEN 0 ELSE 1 END, id ASC
        LIMIT :limit
    """
    result = await session.execute(text(sql), params)
    return [_candidate_from_row(row) for row in result.mappings().all()]


async def _refine_document_candidate(
    session: Any,
    *,
    vector_value: str,
    model: str,
    dimension: int,
    threshold: float,
    limit: int,
    document_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    file_id = await _closest_file_for_document(
        session,
        vector_value=vector_value,
        model=model,
        dimension=dimension,
        document_id=document_id,
    )
    path = [{"level": "document", "id": document_id}]
    if file_id is not None:
        path.append({"level": "file", "id": file_id})
    children = await _hierarchical_children(
        session,
        vector_value=vector_value,
        model=model,
        dimension=dimension,
        threshold=threshold,
        limit=limit,
        document_id=document_id,
        file_id=file_id,
    )
    return children, path


async def _refine_file_candidate(
    session: Any,
    *,
    vector_value: str,
    model: str,
    dimension: int,
    threshold: float,
    limit: int,
    file_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    document_id = await _closest_document_for_file(
        session,
        vector_value=vector_value,
        model=model,
        dimension=dimension,
        file_id=file_id,
    )
    path = [{"level": "file", "id": file_id}]
    if document_id is not None:
        path.append({"level": "document", "id": document_id})
    children = await _hierarchical_children(
        session,
        vector_value=vector_value,
        model=model,
        dimension=dimension,
        threshold=threshold,
        limit=limit,
        document_id=document_id,
        file_id=file_id,
    )
    return children, path


async def _closest_file_for_document(
    session: Any,
    *,
    vector_value: str,
    model: str,
    dimension: int,
    document_id: str,
) -> str | None:
    result = await session.execute(
        text(
            "SELECT f.id::text AS id "
            "FROM documents AS d "
            "JOIN files AS f ON f.id = d.owner_id OR f.owner_id = d.id "
            "JOIN semantic_chunk_embeddings AS sce ON sce.entity_type = 'file' AND sce.entity_id = f.id "
            "WHERE d.id = :document_id AND d.deleted_at IS NULL AND f.deleted_at IS NULL "
            "AND sce.active IS TRUE AND sce.model = :model AND sce.dimension = :dimension "
            "ORDER BY 1.0 - (sce.vector <=> CAST(:query_vector AS vector)) DESC "
            "LIMIT 1"
        ),
        {"document_id": document_id, "query_vector": vector_value, "model": model, "dimension": dimension},
    )
    return result.scalar_one_or_none()


async def _closest_document_for_file(
    session: Any,
    *,
    vector_value: str,
    model: str,
    dimension: int,
    file_id: str,
) -> str | None:
    result = await session.execute(
        text(
            "SELECT d.id::text AS id "
            "FROM documents AS d "
            "JOIN semantic_chunk_embeddings AS sce ON sce.entity_type = 'document' AND sce.entity_id = d.id "
            "WHERE d.owner_id = :file_id AND d.deleted_at IS NULL "
            "AND sce.active IS TRUE AND sce.model = :model AND sce.dimension = :dimension "
            "ORDER BY 1.0 - (sce.vector <=> CAST(:query_vector AS vector)) DESC "
            "LIMIT 1"
        ),
        {"file_id": file_id, "query_vector": vector_value, "model": model, "dimension": dimension},
    )
    return result.scalar_one_or_none()


def _candidate_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    level = str(row["level"])
    entity_id = str(row["id"])
    score = float(row["score"])
    return {
        "key": f"{level}:{entity_id}",
        "id": entity_id,
        "level": level,
        "preview": row.get("preview") or "",
        "score": score,
        "similarity": score,
        "distance": 1.0 - score,
        "document_id": str(row["document_id"]) if row.get("document_id") is not None else None,
        "file_id": str(row["file_id"]) if row.get("file_id") is not None else None,
        "paragraph_id": str(row["paragraph_id"]) if row.get("paragraph_id") is not None else None,
        "chunk_id": str(row["chunk_id"]) if row.get("chunk_id") is not None else None,
        "parent_path": _parent_path(row),
    }


def _parent_path(row: Mapping[str, Any]) -> list[dict[str, str]]:
    path: list[dict[str, str]] = []
    for level, key in (("file", "file_id"), ("document", "document_id"), ("paragraph", "paragraph_id")):
        value = row.get(key)
        if value is not None:
            path.append({"level": level, "id": str(value)})
    chunk_id = row.get("chunk_id")
    if chunk_id is not None:
        path.append({"level": "semantic_chunk", "id": str(chunk_id)})
    return path


def _merge_window(window: list[dict[str, Any]], additions: Sequence[dict[str, Any]], limit: int) -> None:
    by_key = {item["key"]: item for item in window}
    for item in additions:
        existing = by_key.get(item["key"])
        if existing is None or float(item["score"]) > float(existing["score"]):
            by_key[item["key"]] = item
    ordered = sorted(by_key.values(), key=lambda item: (-float(item["score"]), item["level"], item["id"]))
    window[:] = ordered[:limit]


def _semantic_refinement_options(
    value: Any,
    defaults: RuntimeSemanticRefinementConfig,
) -> dict[str, Any]:
    base = defaults.as_dict()
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise RuntimeError("semantic_refinement must be an object")
    enabled = _bool_refinement_option(value.get("enabled", base["enabled"]), "semantic_refinement.enabled")
    threshold = _bounded_float(
        value.get("threshold", base["threshold"]),
        "semantic_refinement.threshold",
        0.0,
        1.0,
    )
    candidate_limit = _bounded_int_option(
        value.get("candidate_limit", base["candidate_limit"]),
        "semantic_refinement.candidate_limit",
        1,
        1000,
    )
    result_limit = _bounded_int_option(
        value.get("result_limit", base["result_limit"]),
        "semantic_refinement.result_limit",
        1,
        1000,
    )
    return {
        "enabled": enabled,
        "threshold": threshold,
        "candidate_limit": candidate_limit,
        "result_limit": result_limit,
        "diagnostics": _bool_refinement_option(
            value.get("diagnostics", base["diagnostics"]),
            "semantic_refinement.diagnostics",
        ),
    }


def _bool_refinement_option(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise RuntimeError(f"{name} must be a boolean")


def _bounded_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if result < minimum or result > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return result


def _bounded_int_option(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return result


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
