"""Focused contract tests for pure adapter-backed hybrid fusion."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from chunk_metadata_adapter import (
    ChunkQueryResponse,
    HybridSearchConfig,
    HybridSearchHelper,
    HybridStrategy,
    SearchResult,
    SemanticChunk,
)

import doc_store_server.query.hybrid as hybrid
from doc_store_server.query.hybrid import (
    HybridFusionError,
    MalformedHybridCandidateError,
    UnsupportedHybridStrategyError,
    build_hybrid_response,
    fuse_search_results,
    serialize_hybrid_response,
)

CHUNK_A = "11111111-1111-4111-8111-111111111111"
CHUNK_B = "22222222-2222-4222-8222-222222222222"
CHUNK_C = "33333333-3333-4333-8333-333333333333"


def _chunk(chunk_id: str) -> SemanticChunk:
    return SemanticChunk(
        uuid=chunk_id,
        type="DocBlock",
        body=f"body for {chunk_id}",
        text=f"text for {chunk_id}",
        block_meta={"source": "fixture", "chunk_id": chunk_id},
    )


def _result(
    chunk_id: str,
    *,
    bm25: float | None = None,
    semantic: float | None = None,
    rank: int = 1,
    matched_fields: list[str] | None = None,
    highlights: dict[str, list[str]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        chunk=_chunk(chunk_id),
        bm25_score=bm25,
        semantic_score=semantic,
        rank=rank,
        matched_fields=matched_fields,
        highlights=highlights,
        search_metadata=metadata,
    )


def _candidates() -> tuple[list[SearchResult], list[SearchResult]]:
    full_text = [
        _result(
            CHUNK_B,
            bm25=0.80,
            rank=2,
            matched_fields=["body", "title"],
            highlights={"body": ["<b>needle</b>"]},
            metadata={"full_text": "kept", "shared": "full"},
        ),
        _result(
            CHUNK_A,
            bm25=0.80,
            rank=1,
            matched_fields=["summary"],
            highlights={"summary": ["summary highlight"]},
            metadata={"full_only": True},
        ),
    ]
    semantic = [
        _result(
            CHUNK_B,
            semantic=0.70,
            rank=1,
            matched_fields=["title", "semantic"],
            highlights={"title": ["semantic highlight"]},
            metadata={"semantic": "kept", "shared": "semantic"},
        ),
        _result(
            CHUNK_C,
            semantic=0.95,
            rank=2,
            matched_fields=["body"],
            highlights={"body": ["semantic-only"]},
            metadata={"semantic_only": True},
        ),
    ]
    return full_text, semantic


@pytest.mark.parametrize("strategy", list(HybridStrategy))
def test_all_adapter_strategies_fuse_standard_candidates_deterministically(
    strategy: HybridStrategy,
) -> None:
    full_text, semantic = _candidates()
    config = HybridSearchConfig(
        strategy=strategy,
        bm25_weight=0.7,
        semantic_weight=0.3,
        normalize_scores=False,
    )

    first = fuse_search_results(full_text, semantic, config=config)
    second = fuse_search_results(list(reversed(full_text)), list(reversed(semantic)), config=config)

    assert [result.chunk_id for result in first] == [result.chunk_id for result in second]
    assert [result.rank for result in first] == [1, 2, 3]
    assert all(type(result) is SearchResult for result in first)
    assert all(result.hybrid_score is not None for result in first)
    assert all(result.search_metadata["fusion_strategy"] == strategy.value for result in first)
    assert all("fusion_score" in result.search_metadata for result in first)


def test_weighting_rank_limit_and_tie_breaker_are_admitted_and_stable() -> None:
    full_text, semantic = _candidates()
    config = HybridSearchConfig(
        strategy=HybridStrategy.WEIGHTED_SUM,
        bm25_weight=1.0,
        semantic_weight=0.0,
        normalize_scores=False,
    )

    result = fuse_search_results(full_text, semantic, config=config, limit=2, tie_breaker="chunk_id")

    assert [item.chunk_id for item in result] == [CHUNK_A, CHUNK_B]
    assert [item.rank for item in result] == [1, 2]
    assert result[0].hybrid_score == pytest.approx(0.8)
    assert result[1].hybrid_score == pytest.approx(0.8)

    rank_tied = fuse_search_results(
        full_text,
        semantic,
        config=config,
        tie_breaker="rank",
    )
    assert [item.chunk_id for item in rank_tied[:2]] == [CHUNK_B, CHUNK_A]


def test_duplicate_identity_is_merged_with_evidence_and_original_scores_preserved() -> None:
    full_text, semantic = _candidates()
    result = fuse_search_results(
        full_text,
        semantic,
        config=HybridSearchConfig(normalize_scores=False),
    )[0]

    assert result.chunk_id == CHUNK_B
    assert result.bm25_score == 0.8
    assert result.semantic_score == 0.7
    assert result.matched_fields == ["body", "title", "semantic"]
    assert result.highlights == {
        "body": ["<b>needle</b>"],
        "title": ["semantic highlight"],
    }
    assert result.search_metadata == {
        "full_text": "kept",
        "shared": "semantic",
        "semantic": "kept",
        "fusion_strategy": "weighted_sum",
        "fusion_score": pytest.approx(0.75),
    }


def test_response_is_built_and_serialized_only_through_adapter_factories() -> None:
    full_text, semantic = _candidates()
    results = fuse_search_results(full_text, semantic, config=HybridSearchConfig())

    response = build_hybrid_response(results)
    serialized = serialize_hybrid_response(results)

    assert type(response) is ChunkQueryResponse
    assert [item.chunk_id for item in response.results] == [item.chunk_id for item in results]
    assert serialized["status"] == "success"
    assert [item["chunk_id"] for item in serialized["results"]] == [item.chunk_id for item in results]
    assert serialized["results"][0]["search_metadata"]["fusion_score"] == pytest.approx(
        results[0].search_metadata["fusion_score"]
    )


def test_unsupported_strategy_and_invalid_limits_or_ties_are_rejected() -> None:
    full_text, semantic = _candidates()
    config = HybridSearchConfig()
    config.strategy = "legacy"  # type: ignore[assignment]

    with pytest.raises(UnsupportedHybridStrategyError):
        fuse_search_results(full_text, semantic, config=config)
    with pytest.raises(HybridFusionError):
        fuse_search_results(full_text, semantic, config=HybridSearchConfig(), limit=-1)
    with pytest.raises(HybridFusionError):
        fuse_search_results(full_text, semantic, config=HybridSearchConfig(), tie_breaker="score")
    with pytest.raises(HybridFusionError):
        fuse_search_results(full_text, semantic, config=object())  # type: ignore[arg-type]


def test_nonstandard_candidates_are_rejected() -> None:
    candidates: list[Any] = [object()]
    invalid_rank = _result(CHUNK_A, bm25=0.5)
    invalid_rank.rank = -1
    candidates.append(invalid_rank)
    invalid_score = _result(CHUNK_B, bm25=0.5)
    invalid_score.bm25_score = float("nan")
    candidates.append(invalid_score)
    invalid_id = _result(CHUNK_C, bm25=0.5)
    invalid_id.chunk_id = ""
    candidates.append(invalid_id)

    for candidate in candidates:
        with pytest.raises(MalformedHybridCandidateError):
            fuse_search_results([candidate], [], config=HybridSearchConfig())


def test_hybrid_has_zero_retrieval_or_competing_query_surface() -> None:
    source = inspect.getsource(hybrid)
    for forbidden in (
        "from doc_store_server.query.compiler",
        "EmbeddingClient",
        "pgvector",
        "SELECT",
        "sqlalchemy",
        "ResponseModel",
        "Repository",
        "FullTextExecutor",
        "SemanticExecutor",
    ):
        assert forbidden not in source
    assert "HybridSearchHelper" in source
    assert type(hybrid.SearchResult) is type(SearchResult)
    assert not any(name.endswith(("Repository", "ResponseModel")) for name in vars(hybrid))
