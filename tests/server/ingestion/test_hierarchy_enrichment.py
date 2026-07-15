"""Tests for the canonical accepted-input hierarchy enrichment boundary."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from chunk_metadata_adapter import SemanticChunk as AdapterSemanticChunk

import doc_store_server.ingestion.hierarchy_enrichment as enrichment_module
from doc_store_server.domain.models import Document
from doc_store_server.ingestion.hierarchy_enrichment import (
    ChapterInput,
    HierarchyEnrichmentError,
    ParagraphInput,
    enrich_hierarchy,
)


SOURCE_UPLOAD_ID = uuid4()
SOURCE_VERSION = "content-version-7"


def _public_chunk(body: str, start: int, end: int, ordinal: int) -> AdapterSemanticChunk:
    """Create an already accepted chunk through the public adapter factory."""

    return AdapterSemanticChunk.from_dict_with_autofill_and_validation(
        {
            "uuid": str(uuid4()),
            "source_id": str(uuid4()),
            "block_id": str(uuid4()),
            "type": "DocBlock",
            "body": body,
            "text": body,
            "ordinal": ordinal,
            "start": start,
            "end": end,
            "block_meta": {"chapter_id": str(uuid4()), "accepted": True},
        }
    )


def _accepted_inputs() -> tuple[tuple[ChapterInput, ...], tuple[AdapterSemanticChunk, ...]]:
    chunks = (
        _public_chunk("first", 0, 5, 0),
        _public_chunk("chapter one", 5, 16, 1),
        _public_chunk("second", 16, 22, 2),
        _public_chunk("chapter two", 22, 33, 3),
    )
    chapters = (
        ChapterInput(
            start=0,
            end=22,
            paragraphs=(
                ParagraphInput(text="paragraph one", start=0, end=16, chunk_indexes=(0, 1)),
                ParagraphInput(text="paragraph two", start=16, end=22, chunk_indexes=(2,)),
            ),
        ),
        ChapterInput(
            start=22,
            end=33,
            paragraphs=(
                ParagraphInput(text="paragraph three", start=22, end=33, chunk_indexes=(3,)),
            ),
        ),
    )
    return chapters, chunks


def _enrich() -> object:
    chapters, chunks = _accepted_inputs()
    return enrich_hierarchy(
        source_upload_id=SOURCE_UPLOAD_ID,
        source_version=SOURCE_VERSION,
        chapters=chapters,
        semantic_chunks=chunks,
    )


def _uuid4(value: object) -> UUID:
    parsed = UUID(str(value))
    assert parsed.version == 4
    return parsed


def test_enrichment_builds_one_ordered_canonical_hierarchy() -> None:
    aggregate = _enrich()

    assert isinstance(aggregate.document, Document)
    assert len(aggregate.chapters) == 2
    assert len(aggregate.paragraphs) == 3
    assert len(aggregate.chunks) == 4
    assert aggregate.document.chapters == list(aggregate.chapters)
    assert [chapter.paragraphs for chapter in aggregate.chapters] == [
        list(aggregate.paragraphs[:2]),
        list(aggregate.paragraphs[2:]),
    ]
    assert [paragraph.text for paragraph in aggregate.paragraphs] == [
        "paragraph one",
        "paragraph two",
        "paragraph three",
    ]

    _uuid4(aggregate.document.id)
    for chapter in aggregate.chapters:
        _uuid4(chapter.id)
        assert chapter.document is aggregate.document
    for paragraph in aggregate.paragraphs:
        _uuid4(paragraph.id)
        assert paragraph.chapter in aggregate.chapters

    chunk_ids = [_uuid4(chunk.uuid) for chunk in aggregate.chunks]
    assert len(set(chunk_ids)) == len(chunk_ids)
    assert all(isinstance(chunk, AdapterSemanticChunk) for chunk in aggregate.chunks)
    assert all(type(chunk).__module__ == "chunk_metadata_adapter.semantic_chunk" for chunk in aggregate.chunks)


def test_enrichment_preserves_chunk_order_ranges_and_public_hierarchy_metadata() -> None:
    chapters, input_chunks = _accepted_inputs()
    aggregate = enrich_hierarchy(
        source_upload_id=SOURCE_UPLOAD_ID,
        source_version=SOURCE_VERSION,
        chapters=chapters,
        semantic_chunks=input_chunks,
    )

    assert [(chunk.body, chunk.start, chunk.end) for chunk in aggregate.chunks] == [
        (chunk.body, chunk.start, chunk.end) for chunk in input_chunks
    ]
    assert [chunk.ordinal for chunk in aggregate.chunks] == [0, 1, 0, 0]
    assert [chunk.block_index for chunk in aggregate.chunks] == [0, 1, 0, 0]
    assert all(chunk.source_id == str(aggregate.document.id) for chunk in aggregate.chunks)

    expected_paragraph_ids = [
        str(aggregate.paragraphs[0].id),
        str(aggregate.paragraphs[0].id),
        str(aggregate.paragraphs[1].id),
        str(aggregate.paragraphs[2].id),
    ]
    expected_chapter_ids = [
        str(aggregate.chapters[0].id),
        str(aggregate.chapters[0].id),
        str(aggregate.chapters[0].id),
        str(aggregate.chapters[1].id),
    ]
    assert [chunk.block_id for chunk in aggregate.chunks] == expected_paragraph_ids
    assert [chunk.block_meta["chapter_id"] for chunk in aggregate.chunks] == expected_chapter_ids


def test_every_hierarchy_entity_and_chunk_has_upload_and_version_traceability() -> None:
    aggregate = _enrich()
    trace = aggregate.traceability
    expected_ids = (
        (aggregate.document.id,)
        + tuple(chapter.id for chapter in aggregate.chapters)
        + tuple(paragraph.id for paragraph in aggregate.paragraphs)
        + tuple(UUID(str(chunk.uuid)) for chunk in aggregate.chunks)
    )

    assert trace.source_upload_id == SOURCE_UPLOAD_ID
    assert trace.source_version == SOURCE_VERSION
    assert trace.document_id == aggregate.document.id
    assert trace.chapter_ids == tuple(chapter.id for chapter in aggregate.chapters)
    assert trace.paragraph_ids == tuple(paragraph.id for paragraph in aggregate.paragraphs)
    assert trace.chunk_ids == tuple(UUID(str(chunk.uuid)) for chunk in aggregate.chunks)
    assert set(trace.entity_source) == set(expected_ids)
    assert all(trace.entity_source[entity_id] == (SOURCE_UPLOAD_ID, SOURCE_VERSION) for entity_id in expected_ids)


def test_enrichment_keeps_adapter_owned_chunks_without_private_model_duplicates() -> None:
    aggregate = _enrich()

    assert all(type(chunk) is AdapterSemanticChunk for chunk in aggregate.chunks)
    assert enrichment_module.SemanticChunk is AdapterSemanticChunk
    assert not any(
        name in vars(enrichment_module)
        for name in (
            "SemanticChunkModel",
            "SemanticChunkFactory",
            "Persistence",
            "EmbeddingClient",
            "SvoChunkerClient",
            "Transaction",
        )
    )
    assert not any(
        type(value).__name__ in {"Sentence", "AST", "DocumentAST", "DocumentAst"}
        for value in (aggregate.document, *aggregate.chapters, *aggregate.paragraphs, *aggregate.chunks)
    )


@pytest.mark.parametrize(
    ("kwargs", "error_type"),
    [
        ({"source_upload_id": None}, HierarchyEnrichmentError),
        ({"source_version": None}, TypeError),
        ({"source_version": ""}, HierarchyEnrichmentError),
        ({"source_version": 0}, HierarchyEnrichmentError),
        ({"chapters": ()}, HierarchyEnrichmentError),
        ({"semantic_chunks": ()}, HierarchyEnrichmentError),
    ],
)
def test_enrichment_rejects_missing_or_invalid_upload_version_and_required_inputs(
    kwargs: dict[str, object], error_type: type[Exception]
) -> None:
    chapters, chunks = _accepted_inputs()
    arguments: dict[str, object] = {
        "source_upload_id": SOURCE_UPLOAD_ID,
        "source_version": SOURCE_VERSION,
        "chapters": chapters,
        "semantic_chunks": chunks,
    }
    arguments.update(kwargs)

    with pytest.raises(error_type):
        enrich_hierarchy(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "chapters_factory",
    [
        lambda: (
            ChapterInput(
                start=0,
                end=20,
                paragraphs=(ParagraphInput("outside", 0, 21, (0, 1, 2, 3)),),
            ),
        ),
        lambda: (
            ChapterInput(
                start=10,
                end=20,
                paragraphs=(ParagraphInput("overlap", 10, 20, (0,)),),
            ),
            ChapterInput(
                start=15,
                end=25,
                paragraphs=(ParagraphInput("overlap", 15, 25, (1, 2, 3)),),
            ),
        ),
        lambda: (
            ChapterInput(
                start=0,
                end=10,
                paragraphs=(ParagraphInput("missing owner", 0, 10, (0, 2)),),
            ),
        ),
        lambda: (
            ChapterInput(
                start=0,
                end=33,
                paragraphs=(ParagraphInput("unordered owner", 0, 33, (1, 0, 2, 3)),),
            ),
        ),
    ],
)
def test_enrichment_rejects_inconsistent_hierarchy_ownership_order_and_ranges(chapters_factory) -> None:
    _, chunks = _accepted_inputs()

    with pytest.raises(HierarchyEnrichmentError):
        enrich_hierarchy(
            source_upload_id=SOURCE_UPLOAD_ID,
            source_version=SOURCE_VERSION,
            chapters=chapters_factory(),
            semantic_chunks=chunks,
        )


def test_enrichment_has_no_source_acceptance_or_downstream_stage_side_effects() -> None:
    aggregate = _enrich()

    assert not any(
        name in vars(enrichment_module)
        for name in (
            "accept_source",
            "source_normalizer",
            "SvoChunkerClient",
            "EmbeddingClient",
            "SemanticChunkRepository",
            "AsyncSession",
            "transaction",
            "publish",
        )
    )
    assert aggregate.traceability.entity_source
    assert not hasattr(aggregate, "sentences")
    assert not hasattr(aggregate, "ast")
