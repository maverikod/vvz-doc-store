"""Contract tests for the public chunk-metadata-adapter query model."""

from chunk_metadata_adapter import ChunkQuery
from pydantic import ValidationError
import pytest


def test_chunk_query_is_imported_from_the_public_adapter_surface() -> None:
    assert ChunkQuery.__name__ == "ChunkQuery"
    assert ChunkQuery.__module__ == "chunk_metadata_adapter.chunk_query"


def test_chunk_query_preserves_typed_metadata_filters_and_search_parameters() -> None:
    query = ChunkQuery(
        created_at="2026-07-14T12:00:00",
        project="doc-store",
        type="CodeBlock",
        role="assistant",
        language="Python",
        status="indexed",
        year=2026,
        is_public=True,
        tags=["search", "contract"],
        search_query="canonical ChunkQuery contract",
        search_fields=["body", "summary"],
        embedding=[0.1, 0.2, 0.3],
        hybrid_search=True,
        bm25_k1=1.1,
        bm25_b=0.7,
        bm25_weight=0.4,
        semantic_weight=0.6,
        min_score=0.25,
        max_results=25,
    )

    assert query.project == "doc-store"
    assert query.type == "CodeBlock"
    assert query.role == "assistant"
    assert query.language == "Python"
    assert query.status == "indexed"
    assert query.year == 2026
    assert query.is_public is True
    assert query.tags == ["search", "contract"]
    assert query.search_query == "canonical ChunkQuery contract"
    assert query.search_fields == ["body", "summary"]
    assert query.embedding == [0.1, 0.2, 0.3]
    assert query.hybrid_search is True
    assert query.bm25_weight == 0.4
    assert query.semantic_weight == 0.6
    assert query.max_results == 25


def test_chunk_query_public_serialization_and_validation_round_trip() -> None:
    query = ChunkQuery(
        created_at="2026-07-14T12:00:00",
        project="doc-store",
        type="CodeBlock",
        language="Python",
        status="indexed",
        search_query="semantic retrieval",
        embedding=[0.25, 0.5],
        hybrid_search=True,
        bm25_weight=0.35,
        semantic_weight=0.65,
        max_results=10,
    )

    serialized = query.model_dump(mode="json")
    validated = ChunkQuery.model_validate(serialized)
    from_json = ChunkQuery.model_validate_json(query.model_dump_json())

    assert validated.model_dump(mode="json") == serialized
    assert from_json.model_dump(mode="json") == serialized


def test_chunk_query_validation_keeps_public_bounds() -> None:
    with pytest.raises(ValidationError):
        ChunkQuery(max_results=0)

    with pytest.raises(ValidationError):
        ChunkQuery(semantic_weight=1.1)

