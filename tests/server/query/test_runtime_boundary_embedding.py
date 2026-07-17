from __future__ import annotations

import asyncio
from typing import Any

from chunk_metadata_adapter import ChunkQuery

from doc_store_server.query.compiler import ExecutionMode, compile_query
from doc_store_server.query.runtime_boundary import RuntimeSearchBoundary
from doc_store_server.runtime.embedding_config import RuntimeEmbeddingConfig


class _EmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    async def embed(self, texts: list[str], **kwargs: Any) -> dict[str, Any]:
        self.calls.append((texts, dict(kwargs)))
        return {
            "model": "model-a",
            "dimension": 3,
            "results": [{"embedding": [0.1, 0.2, 0.3]}],
        }


class _Mappings:
    def all(self) -> list[dict[str, Any]]:
        return []


class _Result:
    def mappings(self) -> _Mappings:
        return _Mappings()


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.calls.append((statement, params))
        return _Result()


def _config() -> RuntimeEmbeddingConfig:
    return RuntimeEmbeddingConfig(
        protocol="https",
        host="embedding-service",
        port=8001,
        cert=None,
        key=None,
        ca=None,
        check_hostname=False,
        token=None,
        token_header=None,
        timeout=300.0,
        wait_timeout=300,
        poll_interval=1.0,
        provider="embedding-service-vvz",
        model="model-a",
        model_version="v1",
        dimension=3,
        device=None,
        batch_size=16,
        direct_text_max_chars=0,
    )


def test_runtime_search_embeds_semantic_text_query_before_compilation() -> None:
    client = _EmbeddingClient()
    boundary = RuntimeSearchBoundary(
        "postgresql://unused",
        embedding_config=_config(),
        embedding_client=client,
    )

    query = asyncio.run(
        boundary._query_with_runtime_embedding(
            ChunkQuery(
                search_query="semantic query",
                hybrid_search=True,
                bm25_weight=0.0,
                semantic_weight=1.0,
                max_results=5,
            )
        )
    )
    plan = compile_query(query)

    assert plan.mode is ExecutionMode.SEMANTIC
    assert plan.embedding == (0.1, 0.2, 0.3)
    assert plan.search_query is None
    assert client.calls == [
        (
            ["semantic query"],
            {
                "model": "model-a",
                "dimension": 3,
                "wait": True,
                "wait_timeout": 300,
                "poll_interval": 1.0,
                "device": None,
            },
        )
    ]


def test_runtime_search_does_not_embed_plain_full_text_query() -> None:
    client = _EmbeddingClient()
    boundary = RuntimeSearchBoundary(
        "postgresql://unused",
        embedding_config=_config(),
        embedding_client=client,
    )

    query = asyncio.run(boundary._query_with_runtime_embedding(ChunkQuery(search_query="plain query")))
    plan = compile_query(query)

    assert plan.mode is ExecutionMode.FULL_TEXT
    assert plan.search_query == "plain query"
    assert plan.embedding is None
    assert client.calls == []


def test_runtime_search_embeds_explicit_semantic_weight_text_query() -> None:
    client = _EmbeddingClient()
    boundary = RuntimeSearchBoundary(
        "postgresql://unused",
        embedding_config=_config(),
        embedding_client=client,
    )

    query = asyncio.run(
        boundary._query_with_runtime_embedding(
            ChunkQuery(search_query="semantic weighted query", semantic_weight=1.0, max_results=5)
        )
    )
    plan = compile_query(query)

    assert plan.mode is ExecutionMode.SEMANTIC
    assert plan.embedding == (0.1, 0.2, 0.3)
    assert plan.search_query is None
    assert client.calls[0][0] == ["semantic weighted query"]


def test_runtime_structured_search_uses_compiled_limit_and_offset() -> None:
    boundary = RuntimeSearchBoundary("postgresql://unused", embedding_config=_config())
    session = _Session()
    plan = compile_query(
        {
            "block_meta": {"source_name": "7d-55-Периодический_закон_Менделеева.md"},
            "limit": 20,
            "offset": 40,
        }
    )

    results = asyncio.run(boundary._execute_structured(session, plan))

    assert results == ()
    statement, params = session.calls[0]
    sql = str(statement)
    assert params["limit"] == 20
    assert params["offset"] == 40
    assert "LIMIT :limit" in sql
    assert "OFFSET :offset" in sql


def test_runtime_structured_search_filters_classifier_fields() -> None:
    boundary = RuntimeSearchBoundary("postgresql://unused", embedding_config=_config())
    session = _Session()
    plan = compile_query(
        {
            "block_type": "sentence",
            "status": "needs_review",
            "language": "en",
            "category": "uncategorized",
            "limit": 5,
        }
    )

    results = asyncio.run(boundary._execute_structured(session, plan))

    assert results == ()
    statement, params = session.calls[0]
    sql = str(statement)
    assert "bt.descr = :p0" in sql
    assert "cat.descr = :p1" in sql
    assert "lang.descr = :p2" in sql
    assert "cs.descr = :p3" in sql
    assert [params[f"p{index}"] for index in range(4)] == [
        "sentence",
        "uncategorized",
        "en",
        "needs_review",
    ]
