"""Contract tests for the SemanticChunk domain-to-adapter boundary."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from chunk_metadata_adapter import SemanticChunk as AdapterSemanticChunk

from doc_store_server.domain.models import Chapter, Document, Paragraph
from doc_store_server.domain.semantic_chunk import (
    ChunkSpec,
    SemanticChunk,
    build_semantic_chunks,
    deserialize_semantic_chunk,
    serialize_semantic_chunks,
    validate_semantic_chunks,
)


@pytest.fixture
def hierarchy() -> tuple[Document, Chapter, Paragraph]:
    document = Document()
    chapter = Chapter(document=document)
    paragraph = Paragraph(chapter=chapter, text="first chunk second chunk")
    document.chapters.append(chapter)
    chapter.paragraphs.append(paragraph)
    return document, chapter, paragraph


def _chunks(hierarchy: tuple[Document, Chapter, Paragraph]) -> tuple[SemanticChunk, ...]:
    document, chapter, paragraph = hierarchy
    return build_semantic_chunks(
        document,
        chapter,
        paragraph,
        (
            ChunkSpec(body="first", start=0, end=5, ordinal=0),
            ChunkSpec(body="second", start=6, end=12, ordinal=1),
        ),
    )


def test_semantic_chunk_is_the_public_adapter_model() -> None:
    assert SemanticChunk is AdapterSemanticChunk
    assert SemanticChunk.__module__ == "chunk_metadata_adapter.semantic_chunk"


def test_build_maps_identity_and_structural_metadata(hierarchy) -> None:
    document, chapter, paragraph = hierarchy
    chunks = _chunks(hierarchy)

    chunk_ids = [UUID(str(chunk.uuid)) for chunk in chunks]
    assert all(chunk_id.version == 4 for chunk_id in chunk_ids)
    assert len(set(chunk_ids)) == len(chunks)
    assert {chunk.source_id for chunk in chunks} == {str(document.id)}
    assert {chunk.block_id for chunk in chunks} == {str(paragraph.id)}
    assert [chunk.block_meta for chunk in chunks] == [{"chapter_id": str(chapter.id)}] * 2
    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    assert [chunk.block_index for chunk in chunks] == [0, 1]
    assert [(chunk.start, chunk.end) for chunk in chunks] == [(0, 5), (6, 12)]


def test_build_uses_adapter_serialization_factories_for_round_trip(hierarchy) -> None:
    chunks = _chunks(hierarchy)
    serialized = serialize_semantic_chunks(chunks)
    restored = tuple(deserialize_semantic_chunk(payload) for payload in serialized)

    assert all(isinstance(chunk, AdapterSemanticChunk) for chunk in restored)
    assert [chunk.uuid for chunk in restored] == [chunk.uuid for chunk in chunks]
    assert [chunk.model_dump(mode="json") for chunk in restored] == list(serialized)


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        ("chapter_document", "Chapter must belong to the supplied Document"),
        ("paragraph_chapter", "Paragraph must belong to the supplied Chapter"),
    ],
)
def test_build_rejects_invalid_hierarchy_mapping(hierarchy, mapping, message) -> None:
    document, chapter, paragraph = hierarchy
    if mapping == "chapter_document":
        chapter = Chapter(document=Document())
    else:
        chapter = Chapter(document=document)
        paragraph = Paragraph(chapter=Chapter(document=document))

    with pytest.raises(ValueError, match=message):
        build_semantic_chunks(
            document,
            chapter,
            paragraph,
            (ChunkSpec(body="chunk", start=0, end=5),),
        )


@pytest.mark.parametrize(
    "specs",
    [
        (ChunkSpec(body="one", start=0, end=3, uuid=uuid4()),
         ChunkSpec(body="two", start=4, end=7, uuid=None)),
        (ChunkSpec(body="one", start=0, end=3, ordinal=1),),
    ],
)
def test_build_rejects_duplicate_or_unstable_ordering(hierarchy, specs) -> None:
    if specs[0].uuid is not None:
        specs = (specs[0], ChunkSpec(body="two", start=4, end=7, uuid=specs[0].uuid))

    with pytest.raises(ValueError, match="(distinct UUID4|stable input order)"):
        build_semantic_chunks(*hierarchy, specs)


@pytest.mark.parametrize(
    "spec",
    [
        ChunkSpec(body="chunk", start=-1, end=3),
        ChunkSpec(body="chunk", start=3, end=3),
        ChunkSpec(body="chunk", start=4, end=3),
    ],
)
def test_build_rejects_invalid_ranges(hierarchy, spec) -> None:
    with pytest.raises(ValueError, match="chunk range"):
        build_semantic_chunks(*hierarchy, (spec,))


def test_build_rejects_overlapping_ranges(hierarchy) -> None:
    with pytest.raises(ValueError, match="ordered and non-overlapping"):
        build_semantic_chunks(
            *hierarchy,
            (
                ChunkSpec(body="first", start=0, end=5),
                ChunkSpec(body="overlap", start=4, end=8),
            ),
        )


def test_validate_rejects_identity_and_order_metadata_regressions(hierarchy) -> None:
    document, chapter, paragraph = hierarchy
    chunks = list(_chunks(hierarchy))

    chunks[0] = chunks[0].model_copy(update={"source_id": str(uuid4())})
    with pytest.raises(ValueError, match="source_id"):
        validate_semantic_chunks(document, chapter, paragraph, chunks)

    chunks = list(_chunks(hierarchy))
    chunks[1] = chunks[1].model_copy(update={"ordinal": 0})
    with pytest.raises(ValueError, match="order metadata"):
        validate_semantic_chunks(document, chapter, paragraph, chunks)


def test_domain_module_has_no_competing_model_or_storage_pipeline_surface() -> None:
    import doc_store_server.domain.semantic_chunk as module

    assert module.SemanticChunk is AdapterSemanticChunk
    assert not any(name in vars(module) for name in ("Sentence", "AST", "Repository", "Projection"))
    assert not any(name in vars(module) for name in ("Migration", "Persistence", "SemanticChunkModel"))
