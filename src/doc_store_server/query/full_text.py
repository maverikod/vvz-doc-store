"""Execution of already compiled PostgreSQL full-text query plans."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from chunk_metadata_adapter import SearchResult, SemanticChunk

from .compiler import ExecutionMode, ExecutionPlan


class FullTextExecutionError(ValueError):
    """Base error for full-text execution and row-shape failures."""


class NonFullTextPlanError(FullTextExecutionError):
    """Raised when an executor receives a plan for another retrieval mode."""


class MalformedFullTextRowError(FullTextExecutionError):
    """Raised when a database row cannot be mapped to the adapter contract."""


_FIELDS = ("body", "text", "summary", "title")
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


def _field_expression(field: str) -> str:
    if field not in _FIELDS:
        raise FullTextExecutionError(f"unsupported full-text field: {field}")
    return {
        "body": "coalesce(sc.text, '')",
        "text": "coalesce(sc.text, '')",
        "summary": "coalesce(sc.block_meta ->> 'summary', '')",
        "title": "coalesce(d.title, '')",
    }[field]


def _search_vector(fields: tuple[str, ...]) -> str:
    selected = fields or _FIELDS
    parts = []
    for field in selected:
        weight = {"title": "A", "summary": "B", "body": "C", "text": "C"}[field]
        parts.append(f"setweight(to_tsvector('simple', {_field_expression(field)}), '{weight}')")
    return " || ".join(parts)


def _predicate_sql(plan: ExecutionPlan, params: dict[str, Any]) -> list[str]:
    clauses: list[str] = []
    for predicate in plan.predicates.predicates:
        column = _PREDICATE_COLUMNS.get(predicate.column)
        if column is None and predicate.column in {"tags", "links", "block_meta"}:
            column = f"sc.block_meta -> '{predicate.column}'"
        if column is None:
            # The compiler owns the allowlist.  Refuse drift rather than turning
            # a compiled predicate into dynamically interpolated SQL.
            raise FullTextExecutionError(f"unsupported compiled predicate: {predicate.column}")
        if predicate.operator not in {"=", "@>"}:
            raise FullTextExecutionError(f"unsupported compiled operator: {predicate.operator}")
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
        raise MalformedFullTextRowError(f"database row is missing {name!r}") from exc


def _chunk_from_row(row: Mapping[str, Any]) -> SemanticChunk:
    supplied = row.get("chunk")
    if isinstance(supplied, SemanticChunk):
        return supplied
    payload = row.get("chunk_payload")
    if not isinstance(payload, Mapping):
        block_meta = _row_value(row, "block_meta")
        if not isinstance(block_meta, Mapping):
            raise MalformedFullTextRowError("database row block_meta must be a mapping")
        payload = {
            "uuid": str(_row_value(row, "id")),
            "source_id": str(_row_value(row, "document_id")),
            "block_id": str(_row_value(row, "paragraph_id")),
            "type": _row_value(row, "chunk_type") or "DocBlock",
            "body": _row_value(row, "text"),
            "text": _row_value(row, "text"),
            "ordinal": _row_value(row, "order_index"),
            "start": _row_value(row, "source_start"),
            "end": _row_value(row, "source_end"),
            "block_meta": dict(block_meta),
        }
    try:
        return SemanticChunk.from_dict_with_autofill_and_validation(dict(payload))
    except Exception as exc:
        raise MalformedFullTextRowError("database row is not a valid SemanticChunk") from exc


def _result_from_row(row: Mapping[str, Any], rank: int) -> SearchResult:
    try:
        score = float(_row_value(row, "relevance"))
        score = max(0.0, min(1.0, score))
        matched = list(_row_value(row, "matched_fields") or [])
        highlights = dict(_row_value(row, "highlights") or {})
        chunk = _chunk_from_row(row)
        return SearchResult(
            chunk_id=str(_row_value(row, "id")),
            chunk=chunk,
            bm25_score=score,
            rank=rank,
            matched_fields=matched,
            highlights=highlights,
            search_metadata={"relevance": score},
        )
    except MalformedFullTextRowError:
        raise
    except Exception as exc:
        raise MalformedFullTextRowError("database row cannot be mapped to SearchResult") from exc


def _statement(plan: ExecutionPlan) -> tuple[Any, dict[str, Any]]:
    if plan.mode is not ExecutionMode.FULL_TEXT:
        raise NonFullTextPlanError(f"expected full_text plan, got {plan.mode.value}")
    if not plan.text or not plan.text.strip():
        raise FullTextExecutionError("full-text plan must contain a non-empty search text")

    params: dict[str, Any] = {"query_text": plan.text}
    vector = _search_vector(plan.search_fields)
    query = "plainto_tsquery('simple', :query_text)"
    matched = ", ".join(
        f"CASE WHEN to_tsvector('simple', {_field_expression(field)}) @@ {query} "
        f"THEN '{field}' END" for field in (plan.search_fields or _FIELDS)
    )
    selected_fields = plan.search_fields or _FIELDS
    highlight_pairs = ", ".join(
        f"'{field}', CASE WHEN to_tsvector('simple', {_field_expression(field)}) @@ {query} "
        f"THEN ts_headline('simple', {_field_expression(field)}, {query}, 'MaxFragments=2,MaxWords=35') "
        "ELSE NULL END"
        for field in selected_fields
    )
    predicates = _predicate_sql(plan, params)
    where = [f"{vector} @@ {query}", "sc.deleted_at IS NULL"] + predicates
    limit = " LIMIT :limit" if plan.limit is not None else ""
    if plan.limit is not None:
        params["limit"] = plan.limit
    offset = " OFFSET :offset" if plan.offset else ""
    if plan.offset:
        params["offset"] = plan.offset
    sql = f"""
        SELECT sc.id, sc.document_id, sc.paragraph_id, sc.chapter_id,
               sc.order_index, sc.text, sc.source_start, sc.source_end,
               sc.char_count, sc.chunk_type, sc.block_meta,
               (ts_rank_cd({vector}, {query}) /
                (1.0 + ts_rank_cd({vector}, {query}))) AS relevance,
               ARRAY_REMOVE(ARRAY[{matched}], NULL) AS matched_fields,
               jsonb_strip_nulls(jsonb_build_object({highlight_pairs})) AS highlights
        FROM semantic_chunks AS sc
        JOIN documents AS d ON d.id = sc.document_id
        WHERE {' AND '.join(where)}
        ORDER BY relevance DESC, {', '.join(_order_sql(plan.order_by))}
        {limit}{offset}
    """
    return text(sql), params


def _order_sql(order_by: tuple[str, ...]) -> tuple[str, ...]:
    allowed = {
        "relevance": "relevance DESC",
        "score": "sc.score DESC NULLS LAST",
        "ordinal": "sc.order_index ASC",
        "order_index": "sc.order_index ASC",
        "title": "d.title ASC",
        "uuid": "sc.id ASC",
    }
    terms = tuple(allowed[field] for field in order_by if field in allowed)
    return terms + ("sc.order_index ASC", "sc.id ASC")


class FullTextExecutor:
    """Execute one compiled full-text plan using an injected SQLAlchemy session."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def execute(self, plan: ExecutionPlan) -> tuple[SearchResult, ...]:
        statement, params = _statement(plan)
        result = await self._session.execute(statement, params)
        rows = result.mappings().all()
        return tuple(_result_from_row(row, index) for index, row in enumerate(rows, start=1))


async def execute_full_text(plan: ExecutionPlan, session: Any) -> tuple[SearchResult, ...]:
    """Execute a compiled full-text plan and return adapter SearchResult values."""

    return await FullTextExecutor(session).execute(plan)


search_full_text = execute_full_text

__all__ = [
    "FullTextExecutionError",
    "FullTextExecutor",
    "MalformedFullTextRowError",
    "NonFullTextPlanError",
    "execute_full_text",
    "search_full_text",
]
