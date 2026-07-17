"""Focused tests for the compiled PostgreSQL full-text executor."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from typing import Any

import pytest
from chunk_metadata_adapter import SearchResult, SemanticChunk

from doc_store_server.query.compiler import ExecutionMode, compile_query
from doc_store_server.query.full_text import (
    FullTextExecutor,
    MalformedFullTextRowError,
    NonFullTextPlanError,
)
import doc_store_server.query.full_text as full_text


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeAsyncSession:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any]) -> FakeResult:
        self.calls.append((statement, params))
        return FakeResult(self.rows)


def _plan(**values: Any) -> Any:
    return compile_query({"search_query": "needle", **values})


def _row(
    chunk_id: str = "11111111-1111-4111-8111-111111111111",
    *,
    relevance: float = 0.8,
    matched_fields: list[str] | None = None,
    highlights: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "document_id": "22222222-2222-4222-8222-222222222222",
        "paragraph_id": "33333333-3333-4333-8333-333333333333",
        "order_index": 4,
        "text": "body and normalized text",
        "source_start": 0,
        "source_end": 25,
        "block_meta": {
            "project": "doc-store",
            "summary": "a short summary",
            "title": "a title",
            "source": "fixture",
            "type": "DocBlock",
            "role": "system",
            "status": "indexed",
            "block_type": "paragraph",
            "language": "UNKNOWN",
            "category": "uncategorized",
        },
        "chunk_type": "DocBlock",
        "chunk_type_descr": "DocBlock",
        "role_descr": "system",
        "status_descr": "indexed",
        "block_type_descr": "paragraph",
        "language_descr": "UNKNOWN",
        "category_descr": "uncategorized",
        "relevance": relevance,
        "matched_fields": matched_fields or ["body", "text", "summary", "title"],
        "highlights": highlights or {"body": ["body <b>needle</b> text"]},
    }


def test_full_text_binds_allowlisted_metadata_and_searches_all_canonical_fields() -> None:
    session = FakeAsyncSession([])
    plan = _plan(project="doc-store", tags=["docs", "search"], block_meta={"scope": "client"})

    import asyncio

    asyncio.run(FullTextExecutor(session).execute(plan))

    statement, params = session.calls[0]
    sql = str(statement)
    assert params["query_text"] == "needle"
    assert json.loads(params["p0"]) == {"scope": "client"}
    assert params["p1"] == "doc-store"
    assert json.loads(params["p2"]) == ["docs", "search"]
    assert params["limit"] == 100
    assert "sc.block_meta @> CAST(:p0 AS jsonb)" in sql
    assert "sc.block_meta ->> 'project' = :p1" in sql
    assert "sc.block_meta -> 'tags' @> CAST(:p2 AS jsonb)" in sql
    assert "JOIN semantic_chunk_texts AS sct ON sct.chunk_uuid = sc.id" in sql
    assert "coalesce(sct.text, '')" in sql
    assert "coalesce(sc.block_meta ->> 'summary', '')" in sql
    assert "coalesce(d.title, '')" in sql
    assert sql.count("plainto_tsquery('simple', :query_text)") >= 4
    assert "ts_headline('simple'" in sql
    assert "MaxFragments=2,MaxWords=35" in sql


def test_full_text_uses_compiled_paging_and_deterministic_order() -> None:
    session = FakeAsyncSession([])
    plan = replace(
        _plan(max_results=2),
        offset=3,
        order_by=("score", "ordinal", "uuid"),
    )

    import asyncio

    asyncio.run(FullTextExecutor(session).execute(plan))

    statement, params = session.calls[0]
    sql = str(statement)
    assert params["limit"] == 2
    assert params["offset"] == 3
    assert "ORDER BY relevance DESC, sc.score DESC NULLS LAST, sc.order_index ASC" in sql
    assert sql.endswith("LIMIT :limit OFFSET :offset\n    ")
    assert "sc.id ASC" in sql


def test_full_text_maps_rows_through_adapter_result_and_preserves_metadata() -> None:
    row = _row(
        relevance=1.25,
        matched_fields=["title", "summary"],
        highlights={"title": ["<b>needle</b> title"], "summary": ["summary"]},
    )
    session = FakeAsyncSession([row])

    import asyncio

    results = asyncio.run(FullTextExecutor(session).execute(_plan()))

    assert len(results) == 1
    result = results[0]
    assert type(result) is SearchResult
    assert result.chunk_id == row["id"]
    assert type(result.chunk) is SemanticChunk
    assert str(result.chunk.uuid) == row["id"]
    assert result.chunk.body == row["text"]
    assert result.chunk.block_meta == row["block_meta"]
    assert result.chunk.type.value == "DocBlock"
    assert result.chunk.role.value == "system"
    assert result.chunk.status.value == "indexed"
    assert result.chunk.block_type.value == "paragraph"
    assert result.chunk.language.value == "UNKNOWN"
    assert result.chunk.category == "uncategorized"
    assert result.bm25_score == 1.0
    assert result.rank == 1
    assert result.matched_fields == ["title", "summary"]
    assert result.highlights == row["highlights"]
    serialized = result.to_dict()
    assert serialized["chunk_id"] == row["id"]
    assert serialized["chunk"]["block_meta"] == row["block_meta"]
    assert serialized["search_metadata"] == {"relevance": 1.0}


@pytest.mark.parametrize("mode", [ExecutionMode.STRUCTURED, ExecutionMode.SEMANTIC, ExecutionMode.HYBRID])
def test_executor_rejects_non_full_text_plans(mode: ExecutionMode) -> None:
    session = FakeAsyncSession([])
    plan = replace(_plan(), mode=mode)

    import asyncio

    with pytest.raises(NonFullTextPlanError, match="expected full_text"):
        asyncio.run(FullTextExecutor(session).execute(plan))
    assert session.calls == []


@pytest.mark.parametrize("missing", ["relevance", "id", "block_meta"])
def test_executor_rejects_malformed_rows(missing: str) -> None:
    row = _row()
    row.pop(missing)
    session = FakeAsyncSession([row])

    import asyncio

    with pytest.raises(MalformedFullTextRowError):
        asyncio.run(FullTextExecutor(session).execute(_plan()))


def test_full_text_executor_has_no_compiler_ast_semantic_or_competing_response_surface() -> None:
    source = inspect.getsource(full_text)
    assert "ChunkQuery" not in source
    assert "QuerySpec" not in source
    assert "Lark" not in source
    assert "ExecutionMode.SEMANTIC" not in source
    assert "ExecutionMode.HYBRID" not in source
    assert "semantic_score" not in source
    assert "hybrid_score" not in source
    assert "ResponseModel" not in source
    assert "BaseModel" not in source
    assert "SearchResult" in source
    assert "SemanticChunk" in source
    assert not any(name.endswith(("Repository", "ResponseModel")) for name in vars(full_text))
