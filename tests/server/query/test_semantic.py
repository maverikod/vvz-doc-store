"""Focused contract tests for the compiled PostgreSQL semantic executor."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from typing import Any
from unittest.mock import create_autospec

import pytest
from chunk_metadata_adapter import SearchResult, SemanticChunk
from embed_client import EmbeddingClient

from doc_store_server.query.compiler import ExecutionMode, compile_query
from doc_store_server.query.semantic import (
    EmbeddingGenerationError,
    MalformedSemanticRowError,
    NonSemanticPlanError,
    SemanticExecutor,
)
import doc_store_server.query.semantic as semantic


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


def _client(response: Any) -> EmbeddingClient:
    client = create_autospec(EmbeddingClient, instance=True)
    client.embed.return_value = response
    return client


def _response(*, vector: list[float] | None = None, **overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "provider": "test-provider",
        "model": "test-model",
        "model_version": "2026-01",
        "dimension": 3,
        "results": [{"embedding": vector or [0.1, 0.2, 0.3]}],
    }
    response.update(overrides)
    return response


def _row(chunk_id: str, *, score: float = 0.8, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    block_meta = {"project": "doc-store", "source": "fixture", "tags": ["semantic"]}
    block_meta.update(metadata or {})
    return {
        "id": chunk_id,
        "document_id": "22222222-2222-4222-8222-222222222222",
        "paragraph_id": "33333333-3333-4333-8333-333333333333",
        "order_index": 4,
        "text": "semantic result body",
        "source_start": 0,
        "source_end": 20,
        "block_meta": block_meta,
        "chunk_type": "DocBlock",
        "semantic_score": score,
    }


def _plan(**values: Any) -> Any:
    return compile_query({"embedding": [1, 2, 3], **values})


def _execute(plan: Any, client: EmbeddingClient, session: FakeAsyncSession) -> tuple[SearchResult, ...]:
    return asyncio.run(
        SemanticExecutor(
            session,
            client,
            provider="test-provider",
            model="test-model",
            model_version="2026-01",
            dimension=3,
        ).execute(plan, "find the semantic result")
    )


def test_query_text_uses_embedding_client_contract_and_validates_provider_model_version_vector_dimension() -> None:
    client = _client(_response())
    session = FakeAsyncSession([])

    _execute(_plan(), client, session)

    client.embed.assert_called_once_with(
        ["find the semantic result"], model="test-model", dimension=3, wait=True
    )
    assert session.calls[0][1]["embedding_model"] == "test-model"
    assert session.calls[0][1]["embedding_dimension"] == 3
    assert session.calls[0][1]["query_vector"] == [0.1, 0.2, 0.3]


def test_semantic_sql_selects_only_active_compatible_vectors_and_binds_typed_predicates() -> None:
    session = FakeAsyncSession([])
    _execute(
        _plan(
            project="doc-store",
            tags=["semantic"],
            quality_score="0.8",
            block_meta={"scope": "client"},
        ),
        _client(_response()),
        session,
    )

    statement, params = session.calls[0]
    sql = str(statement)
    assert "sce.active IS TRUE" in sql
    assert "sce.model = :embedding_model" in sql
    assert "sce.dimension = :embedding_dimension" in sql
    assert "sc.deleted_at IS NULL" in sql
    assert "sc.block_meta @> CAST(:p0 AS jsonb)" in sql
    assert "sc.block_meta ->> 'project' = :p1" in sql
    assert "sc.block_meta ->> 'quality_score' = :p2" in sql
    assert "sc.block_meta -> 'tags' @> CAST(:p3 AS jsonb)" in sql
    assert json.loads(params["p0"]) == {"scope": "client"}
    assert params["p1"] == "doc-store"
    assert params["p2"] == "0.8"


def test_pgvector_ranking_pagination_and_tie_order_are_deterministic() -> None:
    session = FakeAsyncSession([])
    plan = replace(_plan(max_results=2), offset=3, order_by=("semantic_score", "uuid"))

    _execute(plan, _client(_response()), session)

    statement, params = session.calls[0]
    sql = str(statement)
    assert "1.0 - (sce.vector <=> CAST(:query_vector AS vector))" in sql
    assert "ORDER BY semantic_score DESC, sc.id ASC, sc.order_index ASC, sc.id ASC" in sql
    assert params["limit"] == 2
    assert params["offset"] == 3


def test_results_use_adapter_types_and_canonical_semantic_metadata() -> None:
    row = _row("11111111-1111-4111-8111-111111111111", score=1.25, metadata={"title": "A title"})
    results = _execute(_plan(), _client(_response()), FakeAsyncSession([row]))

    result = results[0]
    assert type(result) is SearchResult
    assert type(result.chunk) is SemanticChunk
    assert result.chunk_id == row["id"]
    assert result.chunk.block_meta == row["block_meta"]
    assert result.semantic_score == 1.0
    assert result.rank == 1
    assert result.search_metadata == {"semantic_score": 1.0}


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"provider": "test-provider", "model": "test-model", "model_version": "2026-01", "dimension": 3, "results": []},
        _response(provider="other-provider"),
        _response(model="other-model"),
        _response(model_version="other-version"),
        _response(vector=[0.1, 0.2]),
        _response(dimension=2),
        _response(results=[{"embedding": [0.1, "bad", 0.3]}]),
    ],
)
def test_unavailable_mismatched_and_malformed_query_embeddings_are_rejected(response: Any) -> None:
    client = _client(response)
    session = FakeAsyncSession([])

    with pytest.raises(EmbeddingGenerationError):
        _execute(_plan(), client, session)
    assert session.calls == []


def test_embedding_client_failure_and_no_active_compatible_rows_are_rejected_or_empty() -> None:
    client = _client(RuntimeError("provider unavailable"))
    client.embed.side_effect = RuntimeError("provider unavailable")
    session = FakeAsyncSession([])
    with pytest.raises(EmbeddingGenerationError, match="unavailable"):
        _execute(_plan(), client, session)
    assert session.calls == []

    empty_session = FakeAsyncSession([])
    assert _execute(_plan(), _client(_response()), empty_session) == ()
    assert "sce.active IS TRUE" in str(empty_session.calls[0][0])


@pytest.mark.parametrize("missing", ["id", "block_meta", "semantic_score"])
def test_malformed_rows_are_rejected(missing: str) -> None:
    row = _row("11111111-1111-4111-8111-111111111111")
    row.pop(missing)

    with pytest.raises(MalformedSemanticRowError):
        _execute(_plan(), _client(_response()), FakeAsyncSession([row]))


@pytest.mark.parametrize("mode", [ExecutionMode.STRUCTURED, ExecutionMode.FULL_TEXT, ExecutionMode.HYBRID])
def test_semantic_executor_rejects_other_modes_without_embedding_or_session_work(mode: ExecutionMode) -> None:
    client = _client(_response())
    session = FakeAsyncSession([])

    with pytest.raises(NonSemanticPlanError):
        _execute(replace(_plan(), mode=mode), client, session)
    client.embed.assert_not_called()
    assert session.calls == []


def test_semantic_module_has_no_provider_worker_queue_full_text_hybrid_or_competing_response_surface() -> None:
    source = inspect.getsource(semantic)
    for forbidden in ("Provider", "Worker", "Queue", "FullText", "Hybrid", "hybrid_score", "ResponseModel", "QuerySpec"):
        assert forbidden not in source
    assert "EmbeddingClient" not in source
    assert "SearchResult" in source
    assert "SemanticChunk" in source
