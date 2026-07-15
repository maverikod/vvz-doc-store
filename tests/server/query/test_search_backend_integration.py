"""End-to-end contract checks for the adapter-backed search query chain."""

from __future__ import annotations

import asyncio
import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from chunk_metadata_adapter import (
    ChunkQuery,
    ChunkQueryResponse,
    HybridSearchConfig,
    HybridStrategy,
    SearchResult,
    SemanticChunk,
)
from embed_client import EmbeddingClient

from doc_store_server.query.backend import QueryBackend
from doc_store_server.query.compiler import ExecutionMode, compile_query
from doc_store_server.query.full_text import FullTextExecutor
from doc_store_server.query.hybrid import build_hybrid_response, fuse_search_results
from doc_store_server.query.semantic import SemanticExecutor


ROOT = Path(__file__).resolve().parents[3]
CHUNK_A = "11111111-1111-4111-8111-111111111111"
CHUNK_B = "22222222-2222-4222-8222-222222222222"


class _Mappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Mappings:
        return _Mappings(self._rows)


class _Session:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.calls.append((statement, params))
        return _Result(self.rows)


def _chunk(chunk_id: str, *, title: str = "Search title") -> SemanticChunk:
    return SemanticChunk(
        uuid=chunk_id,
        type="DocBlock",
        body="body needle text",
        text="normalized needle text",
        summary="summary needle",
        title=title,
        source="fixture",
        block_meta={"project": "doc-store", "source": "fixture", "title": title},
    )


def _row(chunk_id: str = CHUNK_A, **scores: Any) -> dict[str, Any]:
    chunk = _chunk(chunk_id)
    return {
        "id": chunk_id,
        "document_id": "33333333-3333-4333-8333-333333333333",
        "paragraph_id": "44444444-4444-4444-8444-444444444444",
        "order_index": 1,
        "text": chunk.text,
        "source_start": 0,
        "source_end": 20,
        "block_meta": chunk.block_meta,
        "chunk_type": "DocBlock",
        "relevance": scores.get("relevance", 0.8),
        "matched_fields": ["body", "text", "summary", "title"],
        "highlights": {"body": ["<b>needle</b> text"], "title": ["<b>needle</b>"]},
        "semantic_score": scores.get("semantic_score", 0.7),
    }


def _result(chunk_id: str, *, bm25: float | None = None, semantic: float | None = None, rank: int = 1) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        chunk=_chunk(chunk_id),
        bm25_score=bm25,
        semantic_score=semantic,
        rank=rank,
        matched_fields=["body", "title"] if bm25 is not None else ["semantic"],
        highlights={"body": ["<b>needle</b>"]},
        search_metadata={"fixture": True},
    )


def test_canonical_chunk_query_selects_all_four_modes_and_rejects_competing_filters() -> None:
    cases = (
        (ChunkQuery(project="doc-store"), ExecutionMode.STRUCTURED),
        (ChunkQuery(search_query="needle", search_fields=["body", "text", "summary", "title"]), ExecutionMode.FULL_TEXT),
        (ChunkQuery(embedding=[0.1, 0.2, 0.3]), ExecutionMode.SEMANTIC),
        (ChunkQuery(search_query="needle", embedding=[0.1, 0.2, 0.3], hybrid_search=True), ExecutionMode.HYBRID),
    )
    for query, mode in cases:
        plan = compile_query(query)
        assert plan.mode is mode
        assert isinstance(query, ChunkQuery)
    with pytest.raises(ValueError, match="filter_expr"):
        compile_query(ChunkQuery(filter_expr="title == 'legacy'"))


def test_full_text_and_semantic_executors_return_standard_results_with_metadata() -> None:
    full_session = _Session([_row(relevance=1.4)])
    full_plan = compile_query(
        ChunkQuery(search_query="needle", search_fields=["body", "text", "summary", "title"], project="doc-store")
    )
    full = asyncio.run(FullTextExecutor(full_session).execute(full_plan))
    assert type(full[0]) is SearchResult
    assert full[0].bm25_score == 1.0
    assert full[0].matched_fields == ["body", "text", "summary", "title"]
    assert full[0].highlights["title"] == ["<b>needle</b>"]
    assert full[0].chunk.block_meta["project"] == "doc-store"
    assert "plainto_tsquery" in str(full_session.calls[0][0])

    class _Embedding:
        def embed(self, texts: list[str], **kwargs: Any) -> dict[str, Any]:
            assert texts == ["needle"]
            assert kwargs == {"model": "model-a", "dimension": 3, "wait": True}
            return {
                "provider": "fixture",
                "model": "model-a",
                "model_version": "v1",
                "dimension": 3,
                "results": [{"embedding": [0.1, 0.2, 0.3]}],
            }

    semantic_session = _Session([_row(semantic_score=0.91)])
    semantic_plan = compile_query(ChunkQuery(embedding=[0.1, 0.2, 0.3], project="doc-store"))
    semantic = asyncio.run(
        SemanticExecutor(
            semantic_session,
            _Embedding(),
            provider="fixture",
            model="model-a",
            model_version="v1",
            dimension=3,
        ).execute(semantic_plan, "needle")
    )
    assert type(semantic[0]) is SearchResult
    assert semantic[0].semantic_score == pytest.approx(0.91)
    statement, params = semantic_session.calls[0]
    assert params["embedding_model"] == "model-a"
    assert params["embedding_dimension"] == 3
    assert "sce.active IS TRUE" in str(statement)
    assert "CAST(:query_vector AS vector)" in str(statement)


def test_hybrid_fuses_adapter_results_and_preserves_evidence() -> None:
    full = [_result(CHUNK_A, bm25=0.8), _result(CHUNK_B, bm25=0.6, rank=2)]
    semantic = [_result(CHUNK_A, semantic=0.9), _result(CHUNK_B, semantic=0.7, rank=2)]
    fused = fuse_search_results(
        full,
        semantic,
        config=HybridSearchConfig(
            strategy=HybridStrategy.WEIGHTED_SUM,
            bm25_weight=0.5,
            semantic_weight=0.5,
            normalize_scores=False,
        ),
    )
    assert all(type(item) is SearchResult for item in fused)
    assert fused[0].chunk_id == CHUNK_A
    assert fused[0].hybrid_score == pytest.approx(0.85)
    assert fused[0].bm25_score == 0.8
    assert fused[0].semantic_score == 0.9
    assert build_hybrid_response(fused).is_success


@pytest.mark.parametrize(
    ("query", "mode"),
    [
        ({"project": "doc-store"}, "structured"),
        ({"search_query": "needle"}, "full_text"),
        ({"embedding": [0.1, 0.2, 0.3]}, "semantic"),
        ({"search_query": "needle", "embedding": [0.1, 0.2, 0.3], "hybrid_search": True}, "hybrid"),
    ],
)
def test_backend_unified_dispatch_preserves_paging_cancellation_timeout_and_compatibility(
    query: dict[str, Any], mode: str
) -> None:
    calls: list[tuple[str, Any, dict[str, Any]]] = []
    expected = _result(CHUNK_A, bm25=0.8)
    cancellation, timeout = object(), object()

    async def owner(plan: Any, **context: Any) -> tuple[SearchResult, ...]:
        calls.append((plan.mode.value, plan, context))
        return (expected,)

    backend = QueryBackend(structured=owner, full_text=owner, semantic=owner, hybrid=owner)
    continuation = {"next": "cursor-2"}
    response = asyncio.run(
        backend.execute(
            compile_query(query),
            session="fake-session",
            limit=2,
            offset=4,
            cursor="cursor-1",
            cancellation=cancellation,
            timeout=timeout,
            continuation=continuation,
        )
    )
    assert type(response) is ChunkQueryResponse
    assert response.is_success
    assert response.results == [expected]
    assert response.metadata == {
        "backend": "postgresql",
        "mode": mode,
        "limit": 2,
        "offset": 4,
        "cursor": "cursor-1",
        "returned_count": 1,
        "continuation": continuation,
        "cancelled": cancellation,
        "timeout": timeout,
        "compatibility": "chunk-metadata-adapter",
    }
    assert len(calls) == 1
    assert calls[0][2]["session"] == "fake-session"
    assert calls[0][2]["cancellation"] is cancellation
    assert calls[0][2]["timeout"] is timeout


def test_backend_normalizes_dispatch_errors_without_masking_cancellation_or_timeout() -> None:
    plan = compile_query({"project": "doc-store"})

    backend = QueryBackend(
        structured=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("owner failed")),
        full_text=lambda *_args, **_kwargs: (),
        semantic=lambda *_args, **_kwargs: (),
        hybrid=lambda *_args, **_kwargs: (),
    )
    error = asyncio.run(backend.execute(plan, session=object()))
    assert type(error) is ChunkQueryResponse
    assert not error.is_success
    assert error.error_message == "structured dispatch_error: owner failed"

    async def cancelled(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    async def timed(*_args: Any, **_kwargs: Any) -> None:
        raise TimeoutError("deadline")

    for owner, expected in ((cancelled, asyncio.CancelledError), (timed, TimeoutError)):
        guarded = QueryBackend(structured=owner, full_text=owner, semantic=owner, hybrid=owner)
        with pytest.raises(expected):
            asyncio.run(guarded.execute(plan, session=object()))


def test_query_chain_has_separated_boundaries_and_no_competing_artifacts() -> None:
    query_root = ROOT / "src" / "doc_store_server" / "query"
    modules = [query_root / name for name in ("compiler.py", "full_text.py", "semantic.py", "hybrid.py", "backend.py")]
    source = "\n".join(path.read_text(encoding="utf-8") for path in modules)
    assert ChunkQuery.__module__ == "chunk_metadata_adapter.chunk_query"
    assert EmbeddingClient.__module__.startswith("embed_client.")
    assert "QuerySpec" not in source
    assert "ResponseModel" not in source
    assert "from lark" not in source.lower()
    assert "import lark" not in source.lower()
    assert not any(
        name in source
        for name in ("FilterParser(", "QueryParser(", "class LocalAstExecutor", "class QueryLanguage")
    )
    assert not any(path.name.endswith(("_query.py", "_query_parser.py")) for path in query_root.rglob("*"))
    assert not any("repository" in path.name.lower() for path in query_root.rglob("*.py"))
    assert "mcp_proxy_adapter" not in (query_root / "backend.py").read_text(encoding="utf-8")
    assert "sqlalchemy" not in (query_root / "hybrid.py").read_text(encoding="utf-8")
    assert "HybridSearchHelper" in (query_root / "hybrid.py").read_text(encoding="utf-8")
    assert inspect.isclass(ChunkQueryResponse)
    for module in modules:
        ast.parse(module.read_text(encoding="utf-8"))
