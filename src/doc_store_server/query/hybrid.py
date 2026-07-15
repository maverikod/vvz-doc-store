"""Pure fusion of already retrieved full-text and semantic candidates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from typing import Any

from chunk_metadata_adapter import (
    ChunkQueryResponse,
    HybridSearchConfig,
    HybridSearchHelper,
    HybridStrategy,
    SearchResponseBuilder,
    SearchResult,
)


class HybridFusionError(ValueError):
    """Base error for invalid hybrid inputs."""


class UnsupportedHybridStrategyError(HybridFusionError):
    """Raised when the compiled strategy is not supported by the adapter helper."""


class MalformedHybridCandidateError(HybridFusionError):
    """Raised when a retrieval result is not a standard adapter SearchResult."""


_SUPPORTED_STRATEGIES = frozenset(
    {HybridStrategy.WEIGHTED_SUM, HybridStrategy.RECIPROCAL_RANK, HybridStrategy.COMB_SUM, HybridStrategy.COMB_MNZ}
)
_TIE_BREAKERS = frozenset({"chunk_id", "rank"})


def _validate_candidates(candidates: Iterable[SearchResult], source: str) -> tuple[SearchResult, ...]:
    values = tuple(candidates)
    for candidate in values:
        if type(candidate) is not SearchResult:
            raise MalformedHybridCandidateError(f"{source} candidates must be SearchResult values")
        if not isinstance(candidate.chunk_id, str) or not candidate.chunk_id:
            raise MalformedHybridCandidateError(f"{source} candidate has an invalid chunk_id")
        if isinstance(candidate.rank, bool) or not isinstance(candidate.rank, int) or candidate.rank < 0:
            raise MalformedHybridCandidateError(f"{source} candidate {candidate.chunk_id!r} has an invalid rank")
        score = candidate.bm25_score if source == "full_text" else candidate.semantic_score
        if score is None or isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(float(score)):
            raise MalformedHybridCandidateError(f"{source} candidate {candidate.chunk_id!r} has no valid score")
    return values


def _merge_mapping(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def _merge_candidate(full_text: SearchResult | None, semantic: SearchResult | None) -> SearchResult:
    primary = full_text or semantic
    assert primary is not None
    matched = list(
        dict.fromkeys(
            (full_text.matched_fields or [] if full_text else [])
            + (semantic.matched_fields or [] if semantic else [])
        )
    )
    highlights = _merge_mapping(full_text.highlights if full_text else None, semantic.highlights if semantic else None)
    metadata = _merge_mapping(full_text.search_metadata if full_text else None, semantic.search_metadata if semantic else None)
    return SearchResult(
        chunk_id=primary.chunk_id,
        chunk=primary.chunk,
        bm25_score=full_text.bm25_score if full_text else None,
        semantic_score=semantic.semantic_score if semantic else None,
        rank=min((item.rank for item in (full_text, semantic) if item is not None), default=0),
        matched_fields=matched,
        highlights=highlights,
        search_metadata=metadata,
    )


def fuse_search_results(
    full_text_candidates: Sequence[SearchResult],
    semantic_candidates: Sequence[SearchResult],
    *,
    config: HybridSearchConfig,
    limit: int | None = None,
    tie_breaker: str = "chunk_id",
) -> tuple[SearchResult, ...]:
    """Fuse standard retrieval candidates without performing retrieval."""
    if not isinstance(config, HybridSearchConfig):
        raise HybridFusionError("config must be an already compiled HybridSearchConfig")
    if config.strategy not in _SUPPORTED_STRATEGIES:
        raise UnsupportedHybridStrategyError(f"unsupported hybrid strategy: {config.strategy!r}")
    if tie_breaker not in _TIE_BREAKERS:
        raise HybridFusionError(f"unsupported tie breaker: {tie_breaker!r}")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise HybridFusionError("limit must be a non-negative integer or None")

    full_text = _validate_candidates(full_text_candidates, "full_text")
    semantic = _validate_candidates(semantic_candidates, "semantic")
    by_id: dict[str, tuple[SearchResult | None, SearchResult | None]] = {}
    for candidate in full_text:
        current = by_id.get(candidate.chunk_id, (None, None))
        by_id[candidate.chunk_id] = (candidate, current[1])
    for candidate in semantic:
        current = by_id.get(candidate.chunk_id, (None, None))
        by_id[candidate.chunk_id] = (current[0], candidate)

    ids = list(by_id)
    merged = [_merge_candidate(*by_id[chunk_id]) for chunk_id in ids]
    bm25_scores = [item.bm25_score or 0.0 for item in merged]
    semantic_scores = [item.semantic_score or 0.0 for item in merged]
    if config.strategy is HybridStrategy.RECIPROCAL_RANK:
        full_ranks = [by_id[item.chunk_id][0].rank if by_id[item.chunk_id][0] else len(full_text) + 1 for item in merged]
        semantic_ranks = [by_id[item.chunk_id][1].rank if by_id[item.chunk_id][1] else len(semantic) + 1 for item in merged]
        fused_scores = HybridSearchHelper.reciprocal_rank(full_ranks, semantic_ranks, config.bm25_weight, config.semantic_weight)
    else:
        fused_scores = HybridSearchHelper.calculate_hybrid_scores(bm25_scores, semantic_scores, config)

    ranked: list[SearchResult] = []
    for item, fused_score in zip(merged, fused_scores):
        bounded_score = max(0.0, min(1.0, float(fused_score)))
        evidence = dict(item.search_metadata or {})
        evidence.update({"fusion_strategy": config.strategy.value, "fusion_score": float(fused_score)})
        ranked.append(SearchResult(**{**item.__dict__, "hybrid_score": bounded_score, "search_metadata": evidence}))
    ranked.sort(key=lambda item: (-float(item.hybrid_score or 0.0), item.chunk_id if tie_breaker == "chunk_id" else item.rank))
    for rank, item in enumerate(ranked[:limit], start=1):
        item.rank = rank
    return tuple(ranked[:limit])


def build_hybrid_response(results: Iterable[SearchResult]) -> ChunkQueryResponse:
    """Build the adapter response object for already fused results."""
    builder = SearchResponseBuilder()
    for result in results:
        if type(result) is not SearchResult:
            raise MalformedHybridCandidateError("response results must be SearchResult values")
        builder.add_result(result)
    return builder.build()


def serialize_hybrid_response(results: Iterable[SearchResult]) -> dict[str, Any]:
    """Serialize fused results through the adapter response factory."""
    return build_hybrid_response(results).to_dict()


execute_hybrid = fuse_search_results
search_hybrid = fuse_search_results


__all__ = [
    "HybridFusionError",
    "MalformedHybridCandidateError",
    "UnsupportedHybridStrategyError",
    "build_hybrid_response",
    "execute_hybrid",
    "fuse_search_results",
    "search_hybrid",
    "serialize_hybrid_response",
]
