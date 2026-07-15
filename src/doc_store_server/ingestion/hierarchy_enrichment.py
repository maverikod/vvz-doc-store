"""Typed construction boundary for the canonical ingestion hierarchy.

This module only enriches already accepted, ordered public chunks.  It does
not accept source data and deliberately has no clients for SVO, embeddings, or
storage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias
from uuid import UUID, uuid4

from chunk_metadata_adapter import SemanticChunk

from doc_store_server.domain.models import Chapter, Document, Paragraph
from doc_store_server.domain.semantic_chunk import (
    ChunkSpec,
    build_semantic_chunks,
    serialize_semantic_chunk,
)


class HierarchyEnrichmentError(ValueError):
    """Raised when accepted hierarchy or traceability input is inconsistent."""


@dataclass(frozen=True, slots=True)
class ParagraphInput:
    """One ordered paragraph and the source chunk positions it owns."""

    text: str
    start: int
    end: int
    chunk_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ChapterInput:
    """One ordered chapter containing ordered paragraph inputs."""

    paragraphs: tuple[ParagraphInput, ...]
    start: int
    end: int
    heading: str | None = None


@dataclass(frozen=True, slots=True)
class SourceVersionTrace:
    """Immutable source traceability for every produced hierarchy entity."""

    source_upload_id: UUID
    source_version: str | int
    document_id: UUID
    chapter_ids: tuple[UUID, ...]
    paragraph_ids: tuple[UUID, ...]
    chunk_ids: tuple[UUID, ...]
    entity_source: Mapping[UUID, tuple[UUID, str | int]]


@dataclass(frozen=True, slots=True)
class CanonicalIngestionAggregate:
    """Immutable canonical hierarchy assembled for one source version."""

    document: Document
    chapters: tuple[Chapter, ...]
    paragraphs: tuple[Paragraph, ...]
    chunks: tuple[SemanticChunk, ...]
    traceability: SourceVersionTrace


HierarchyInput: TypeAlias = Sequence[ChapterInput]


def enrich_hierarchy(
    *,
    source_upload_id: UUID,
    source_version: str | int,
    chapters: HierarchyInput,
    semantic_chunks: Sequence[SemanticChunk],
) -> CanonicalIngestionAggregate:
    """Build and validate one canonical hierarchy from public chunks.

    ``chunk_indexes`` is explicit by design: silently assigning a chunk to a
    paragraph would make source ownership and traceability ambiguous.
    """

    _validate_source(source_upload_id, source_version)
    if not isinstance(semantic_chunks, Sequence) or isinstance(
        semantic_chunks, (str, bytes, bytearray)
    ):
        raise TypeError("semantic_chunks must be a sequence")
    chunks = tuple(semantic_chunks)
    if not chunks:
        raise HierarchyEnrichmentError("semantic_chunks must not be empty")
    if not chapters:
        raise HierarchyEnrichmentError("hierarchy must contain at least one chapter")

    document = Document(id=uuid4())
    built_chapters: list[Chapter] = []
    built_paragraphs: list[Paragraph] = []
    rebuilt_chunks: list[SemanticChunk] = []
    seen_indexes: list[int] = []
    previous_chapter_end = -1

    for chapter_index, chapter_input in enumerate(chapters):
        _validate_range(chapter_input.start, chapter_input.end, "chapter")
        if chapter_input.start < previous_chapter_end:
            raise HierarchyEnrichmentError("chapters must be ordered and non-overlapping")
        if not chapter_input.paragraphs:
            raise HierarchyEnrichmentError("each chapter must contain paragraphs")
        chapter = Chapter(document=document, id=uuid4())
        document.chapters.append(chapter)
        built_chapters.append(chapter)
        previous_paragraph_end = -1

        for paragraph_index, paragraph_input in enumerate(chapter_input.paragraphs):
            _validate_range(paragraph_input.start, paragraph_input.end, "paragraph")
            if paragraph_input.start < chapter_input.start or paragraph_input.end > chapter_input.end:
                raise HierarchyEnrichmentError("paragraph range must be within its chapter")
            if paragraph_input.start < previous_paragraph_end:
                raise HierarchyEnrichmentError("paragraphs must be ordered and non-overlapping")
            if not isinstance(paragraph_input.text, str) or not paragraph_input.text:
                raise HierarchyEnrichmentError("paragraph text must be non-empty")
            indexes = tuple(paragraph_input.chunk_indexes)
            if not indexes or any(not isinstance(index, int) for index in indexes):
                raise HierarchyEnrichmentError("paragraph chunk ownership must be explicit")
            if indexes != tuple(sorted(indexes)) or len(set(indexes)) != len(indexes):
                raise HierarchyEnrichmentError("paragraph chunk indexes must be ordered and distinct")
            if any(index < 0 or index >= len(chunks) for index in indexes):
                raise HierarchyEnrichmentError("paragraph chunk index is out of range")
            seen_indexes.extend(indexes)
            paragraph = Paragraph(chapter=chapter, id=uuid4(), text=paragraph_input.text)
            chapter.paragraphs.append(paragraph)
            built_paragraphs.append(paragraph)
            specs: list[ChunkSpec] = []
            for index in indexes:
                payload = serialize_semantic_chunk(chunks[index])
                body = payload.get("body")
                start = payload.get("start")
                end = payload.get("end")
                if not isinstance(body, str) or not isinstance(start, int) or not isinstance(end, int):
                    raise HierarchyEnrichmentError("chunk body and source range are required")
                if start < paragraph_input.start or end > paragraph_input.end:
                    raise HierarchyEnrichmentError("chunk range must be within its paragraph")
                specs.append(ChunkSpec(body=body, start=start, end=end, ordinal=len(specs)))
            rebuilt_chunks.extend(build_semantic_chunks(document, chapter, paragraph, specs))
            previous_paragraph_end = paragraph_input.end
        previous_chapter_end = chapter_input.end

    if tuple(seen_indexes) != tuple(range(len(chunks))):
        raise HierarchyEnrichmentError("every chunk must have exactly one ordered paragraph owner")
    produced = tuple(rebuilt_chunks)
    source = (source_upload_id, source_version)
    entity_source = {
        entity_id: source
        for entity_id in (
            (document.id,)
            + tuple(chapter.id for chapter in built_chapters)
            + tuple(paragraph.id for paragraph in built_paragraphs)
            + tuple(UUID(str(chunk.uuid)) for chunk in produced)
        )
    }
    trace = SourceVersionTrace(
        source_upload_id=source_upload_id,
        source_version=source_version,
        document_id=document.id,
        chapter_ids=tuple(chapter.id for chapter in built_chapters),
        paragraph_ids=tuple(paragraph.id for paragraph in built_paragraphs),
        chunk_ids=tuple(UUID(str(chunk.uuid)) for chunk in produced),
        entity_source=MappingProxyType(entity_source),
    )
    return CanonicalIngestionAggregate(
        document=document,
        chapters=tuple(built_chapters),
        paragraphs=tuple(built_paragraphs),
        chunks=produced,
        traceability=trace,
    )


def _validate_source(source_upload_id: UUID, source_version: str | int) -> None:
    if not isinstance(source_upload_id, UUID) or source_upload_id.version != 4:
        raise HierarchyEnrichmentError("source_upload_id must be a UUID version 4 value")
    if isinstance(source_version, bool) or not isinstance(source_version, (str, int)):
        raise TypeError("source_version must be a non-empty string or positive integer")
    if (isinstance(source_version, str) and not source_version.strip()) or (
        isinstance(source_version, int) and source_version <= 0
    ):
        raise HierarchyEnrichmentError("source_version must be non-empty or positive")


def _validate_range(start: int, end: int, label: str) -> None:
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        raise HierarchyEnrichmentError(f"{label} range must satisfy 0 <= start < end")


__all__ = (
    "CanonicalIngestionAggregate",
    "ChapterInput",
    "HierarchyEnrichmentError",
    "ParagraphInput",
    "SourceVersionTrace",
    "enrich_hierarchy",
)
