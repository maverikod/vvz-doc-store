"""Execution of compiled semantic PostgreSQL query plans."""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any

from sqlalchemy import text

from chunk_metadata_adapter import SearchResult, SemanticChunk

from .chunk_payload import (
    CHUNK_TEXT_COLUMN_SQL,
    CHUNK_TEXT_JOIN_SQL,
    CHUNK_TEXT_SELECT_SQL,
    CLASSIFIER_JOIN_SQL,
    CLASSIFIER_SELECT_SQL,
    chunk_payload_from_row,
)
from .compiler import ExecutionMode, ExecutionPlan


class SemanticExecutionError(ValueError):
    """Base class for semantic execution and contract failures."""


class NonSemanticPlanError(SemanticExecutionError):
    """Raised when a semantic executor receives another execution mode."""


class EmbeddingGenerationError(SemanticExecutionError):
    """Raised when the embedding service response is unavailable or invalid."""


class MalformedSemanticRowError(SemanticExecutionError):
    """Raised when a database row cannot be mapped to the adapter contract."""


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
}

_METADATA_FIELDS = {
    "block_id", "block_index", "block_meta", "block_type", "boundary_next",
    "boundary_prev", "category", "chunking_version", "cohesion", "coverage",
    "created_at", "embedding_model", "end", "feedback_accepted",
    "feedback_modifications", "feedback_rejected", "is_code_chunk", "is_public",
    "language", "link_parent", "link_related", "metrics", "role", "sha256",
    "source_lines_end", "source_lines_start", "source_path", "start", "status",
    "subtask_id", "tags_flat", "task_id", "type", "unit_id", "used_in_generation",
    "year", "quality_score",
}
for _field in _METADATA_FIELDS:
    _PREDICATE_COLUMNS.setdefault(_field, f"sc.block_meta ->> '{_field}'")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EmbeddingGenerationError(f"embedding response {field} must be a non-empty string")
    return value


def _query_vector(response: Mapping[str, Any]) -> tuple[float, ...]:
    results = response.get("results", response.get("embeddings"))
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes, bytearray)) or len(results) != 1:
        raise EmbeddingGenerationError("embedding response must contain exactly one result")
    item = results[0]
    vector = item.get("embedding", item.get("vector")) if isinstance(item, Mapping) else item
    if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes, bytearray)):
        raise EmbeddingGenerationError("embedding response vector is not a sequence")
    values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
            raise EmbeddingGenerationError("embedding response vector contains an invalid number")
        values.append(float(value))
    if not values:
        raise EmbeddingGenerationError("embedding response vector is empty")
    declared = response.get("dimension", item.get("dimension") if isinstance(item, Mapping) else None)
    if isinstance(declared, bool) or not isinstance(declared, int) or declared <= 0 or declared != len(values):
        raise EmbeddingGenerationError("embedding response dimension does not match the actual vector")
    return tuple(values)


def _predicate_sql(plan: ExecutionPlan, params: dict[str, Any]) -> list[str]:
    clauses: list[str] = []
    for predicate in plan.predicates.predicates:
        column = _PREDICATE_COLUMNS.get(predicate.column)
        if column is None and predicate.column in {"tags", "links", "block_meta"}:
            column = f"sc.block_meta -> '{predicate.column}'"
        if column is None or predicate.operator not in {"=", "@>"}:
            raise SemanticExecutionError(f"unsupported compiled predicate: {predicate.column}")
        params[predicate.parameter] = predicate.value
        if predicate.operator == "@>":
            params[predicate.parameter] = json.dumps(predicate.value)
            clauses.append(f"{column} @> CAST(:{predicate.parameter} AS jsonb)")
        else:
            clauses.append(f"{column} = :{predicate.parameter}")
    return clauses


def _row_value(row: Mapping[str, Any], name: str) -> Any:
    try:
        return row[name]
    except (KeyError, TypeError) as exc:
        raise MalformedSemanticRowError(f"database row is missing {name!r}") from exc


def _chunk_from_row(row: Mapping[str, Any]) -> SemanticChunk:
    supplied = row.get("chunk")
    if isinstance(supplied, SemanticChunk):
        return supplied
    payload = row.get("chunk_payload")
    if not isinstance(payload, Mapping):
        try:
            payload = chunk_payload_from_row(row, _row_value)
        except TypeError as exc:
            raise MalformedSemanticRowError(str(exc)) from exc
    try:
        return SemanticChunk.from_dict_with_autofill_and_validation(dict(payload))
    except Exception as exc:
        raise MalformedSemanticRowError("database row is not a valid SemanticChunk") from exc


def _result_from_row(row: Mapping[str, Any], rank: int) -> SearchResult:
    try:
        score = float(_row_value(row, "semantic_score"))
        if not isfinite(score):
            raise ValueError("semantic score is not finite")
        return SearchResult(
            chunk_id=str(_row_value(row, "id")), chunk=_chunk_from_row(row),
            semantic_score=max(0.0, min(1.0, score)), rank=rank,
            search_metadata={"semantic_score": max(0.0, min(1.0, score))},
        )
    except MalformedSemanticRowError:
        raise
    except Exception as exc:
        raise MalformedSemanticRowError("database row cannot be mapped to SearchResult") from exc


def _order_sql(order_by: tuple[str, ...]) -> tuple[str, ...]:
    allowed = {
        "semantic_score": "semantic_score DESC", "score": "sc.score DESC NULLS LAST",
        "ordinal": "sc.order_index ASC", "order_index": "sc.order_index ASC",
        "title": "d.title ASC", "uuid": "sc.id ASC",
    }
    return tuple(allowed[field] for field in order_by if field in allowed) + ("sc.order_index ASC", "sc.id ASC")


def _statement(plan: ExecutionPlan, vector: tuple[float, ...], *, model: str, dimension: int) -> tuple[Any, dict[str, Any]]:
    if plan.mode is not ExecutionMode.SEMANTIC:
        raise NonSemanticPlanError(f"expected semantic plan, got {plan.mode.value}")
    params: dict[str, Any] = {"query_vector": list(vector), "embedding_model": model, "embedding_dimension": dimension}
    predicates = _predicate_sql(plan, params)
    where = [
        "sc.deleted_at IS NULL", "sce.active IS TRUE", "sce.model = :embedding_model",
        "sce.dimension = :embedding_dimension", *predicates,
    ]
    if plan.min_score is not None:
        params["min_score"] = plan.min_score
        where.append("(1.0 - (sce.vector <=> CAST(:query_vector AS vector))) >= :min_score")
    limit = " LIMIT :limit" if plan.limit is not None else ""
    if plan.limit is not None:
        params["limit"] = plan.limit
    offset = " OFFSET :offset" if plan.offset else ""
    if plan.offset:
        params["offset"] = plan.offset
    sql = f"""
        SELECT sc.id, sc.document_id, sc.paragraph_id, sc.chapter_id,
               sc.order_index, {CHUNK_TEXT_SELECT_SQL}, sc.source_start, sc.source_end,
               sc.char_count, sc.chunk_type, sc.block_meta,
               {CLASSIFIER_SELECT_SQL},
               (1.0 - (sce.vector <=> CAST(:query_vector AS vector))) AS semantic_score
        FROM semantic_chunks AS sc
        {CHUNK_TEXT_JOIN_SQL}
        JOIN semantic_chunk_embeddings AS sce ON sce.chunk_uuid = sc.id
        JOIN documents AS d ON d.id = sc.document_id
        {CLASSIFIER_JOIN_SQL}
        WHERE {' AND '.join(where)}
        ORDER BY {', '.join(_order_sql(plan.order_by))}{limit}{offset}
    """
    return text(sql), params


class SemanticExecutor:
    """Execute one compiled semantic plan with injected session and embed client."""

    def __init__(self, session: Any, embedding_client: Any, *, provider: str, model: str, model_version: str, dimension: int) -> None:
        self._session = session
        self._embedding_client = embedding_client
        self._provider = _required_text(provider, "provider")
        self._model = _required_text(model, "model")
        self._model_version = _required_text(model_version, "model_version")
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise EmbeddingGenerationError("embedding dimension must be a positive integer")
        self._dimension = dimension

    async def execute(self, plan: ExecutionPlan, query_text: str) -> tuple[SearchResult, ...]:
        if plan.mode is not ExecutionMode.SEMANTIC:
            raise NonSemanticPlanError(f"expected semantic plan, got {plan.mode.value}")
        if not isinstance(query_text, str) or not query_text.strip():
            raise EmbeddingGenerationError("query text must be non-empty")
        try:
            response = self._embedding_client.embed([query_text], model=self._model, dimension=self._dimension, wait=True)
            response = await response if inspect.isawaitable(response) else response
        except Exception as exc:
            raise EmbeddingGenerationError("embedding client is unavailable") from exc
        if not isinstance(response, Mapping):
            raise EmbeddingGenerationError("embedding client returned a non-mapping response")
        provider = _required_text(response.get("provider"), "provider")
        model = _required_text(response.get("model"), "model")
        version = _required_text(response.get("model_version"), "model_version")
        if (provider, model, version) != (self._provider, self._model, self._model_version):
            raise EmbeddingGenerationError("embedding response provider/model/version does not match configuration")
        vector = _query_vector(response)
        if len(vector) != self._dimension:
            raise EmbeddingGenerationError("embedding response dimension does not match configuration")
        statement, params = _statement(plan, vector, model=model, dimension=len(vector))
        result = await self._session.execute(statement, params)
        rows = result.mappings().all()
        return tuple(_result_from_row(row, index) for index, row in enumerate(rows, start=1))


async def execute_semantic(plan: ExecutionPlan, session: Any, embedding_client: Any, query_text: str, *, provider: str, model: str, model_version: str, dimension: int) -> tuple[SearchResult, ...]:
    return await SemanticExecutor(session, embedding_client, provider=provider, model=model, model_version=model_version, dimension=dimension).execute(plan, query_text)


search_semantic = execute_semantic

__all__ = ["EmbeddingGenerationError", "MalformedSemanticRowError", "NonSemanticPlanError", "SemanticExecutionError", "SemanticExecutor", "execute_semantic", "search_semantic"]
