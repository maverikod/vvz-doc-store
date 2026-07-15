"""Typed boundary for the canonical SemanticChunk adapter type.

This module owns the hierarchy-to-adapter mapping only.  The adapter remains
the source of truth for the chunk model, validation, and serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence, TypeAlias
from uuid import UUID, uuid4

from chunk_metadata_adapter import BlockType, ChunkType, SemanticChunk as _AdapterSemanticChunk

from .models import Chapter, Document, Paragraph


SemanticChunk = _AdapterSemanticChunk
SemanticChunkPayload: TypeAlias = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ChunkSpec:
    """Input needed to construct one adapter SemanticChunk."""

    body: str
    start: int
    end: int
    uuid: UUID | None = None
    ordinal: int | None = None


def _uuid4(value: UUID, field_name: str) -> str:
    if not isinstance(value, UUID) or value.version != 4:
        raise ValueError(f"{field_name} must be a UUID version 4 value")
    return str(value)


def _validate_hierarchy(document: Document, chapter: Chapter, paragraph: Paragraph) -> None:
    document_id = _uuid4(document.id, "Document.id")
    chapter_id = _uuid4(chapter.id, "Chapter.id")
    paragraph_id = _uuid4(paragraph.id, "Paragraph.id")

    if chapter.document is not document or chapter.document.id != document.id:
        raise ValueError("Chapter must belong to the supplied Document")
    if paragraph.chapter is not chapter or paragraph.chapter.id != chapter.id:
        raise ValueError("Paragraph must belong to the supplied Chapter")

    # Keep the checks explicit: these values are the only hierarchy identities
    # that cross this boundary into adapter metadata.
    del document_id, chapter_id, paragraph_id


def _coerce_spec(value: ChunkSpec | SemanticChunkPayload) -> ChunkSpec:
    if isinstance(value, ChunkSpec):
        return value
    try:
        return ChunkSpec(
            body=value["body"],
            start=value["start"],
            end=value["end"],
            uuid=value.get("uuid"),
            ordinal=value.get("ordinal"),
        )
    except KeyError as exc:
        raise ValueError(f"chunk spec is missing {exc.args[0]!r}") from exc
    except AttributeError as exc:
        raise TypeError("chunk spec must be ChunkSpec or a mapping") from exc


def build_semantic_chunks(
    document: Document,
    chapter: Chapter,
    paragraph: Paragraph,
    chunks: Sequence[ChunkSpec | SemanticChunkPayload],
) -> tuple[SemanticChunk, ...]:
    """Construct ordered adapter chunks for one hierarchy paragraph.

    The adapter factory receives all construction data.  This boundary only
    supplies hierarchy identities, structural parent metadata, and invariants
    that span multiple chunks.
    """

    _validate_hierarchy(document, chapter, paragraph)
    specs = tuple(_coerce_spec(value) for value in chunks)
    if not specs:
        raise ValueError("a Paragraph must produce at least one chunk")

    source_id = _uuid4(document.id, "Document.id")
    block_id = _uuid4(paragraph.id, "Paragraph.id")
    chapter_id = _uuid4(chapter.id, "Chapter.id")
    result: list[SemanticChunk] = []
    seen_uuids: set[str] = set()
    previous_end = -1

    for index, spec in enumerate(specs):
        if not isinstance(spec.body, str) or not spec.body:
            raise ValueError("chunk body must be a non-empty string")
        if not isinstance(spec.start, int) or not isinstance(spec.end, int):
            raise TypeError("chunk range must use integer start and end offsets")
        if spec.start < 0 or spec.end <= spec.start:
            raise ValueError("chunk range must satisfy 0 <= start < end")
        if spec.start < previous_end:
            raise ValueError("chunk ranges must be ordered and non-overlapping")
        if spec.ordinal is not None and spec.ordinal != index:
            raise ValueError("chunk ordinals must match stable input order")

        chunk_uuid = spec.uuid or uuid4()
        chunk_id = _uuid4(chunk_uuid, "SemanticChunk.uuid")
        if chunk_id in seen_uuids:
            raise ValueError("each chunk must have a distinct UUID4")
        seen_uuids.add(chunk_id)

        payload = {
            "uuid": chunk_id,
            "source_id": source_id,
            "block_id": block_id,
            "body": spec.body,
            "text": spec.body,
            "type": ChunkType.DOC_BLOCK,
            "block_type": BlockType.PARAGRAPH,
            "ordinal": index,
            "block_index": index,
            "start": spec.start,
            "end": spec.end,
            "block_meta": {"chapter_id": chapter_id},
        }
        result.append(SemanticChunk.from_dict_with_autofill_and_validation(payload))
        previous_end = spec.end

    return validate_semantic_chunks(document, chapter, paragraph, result)


def validate_semantic_chunk(chunk: SemanticChunk) -> SemanticChunk:
    """Validate an adapter chunk through its public validation factory."""

    if not isinstance(chunk, SemanticChunk):
        raise TypeError("chunk must be chunk_metadata_adapter.SemanticChunk")
    return SemanticChunk.model_validate(chunk)


def validate_semantic_chunks(
    document: Document,
    chapter: Chapter,
    paragraph: Paragraph,
    chunks: Sequence[SemanticChunk],
) -> tuple[SemanticChunk, ...]:
    """Validate one paragraph's ordered adapter chunks and identity mapping."""

    _validate_hierarchy(document, chapter, paragraph)
    source_id = _uuid4(document.id, "Document.id")
    block_id = _uuid4(paragraph.id, "Paragraph.id")
    chapter_id = _uuid4(chapter.id, "Chapter.id")
    validated = tuple(validate_semantic_chunk(chunk) for chunk in chunks)
    if not validated:
        raise ValueError("a Paragraph must produce at least one chunk")

    seen_uuids: set[str] = set()
    previous_end = -1
    for index, chunk in enumerate(validated):
        if chunk.source_id != source_id:
            raise ValueError("SemanticChunk.source_id must equal Document.id")
        if chunk.block_id != block_id:
            raise ValueError("SemanticChunk.block_id must equal Paragraph.id")
        if not isinstance(chunk.block_meta, dict) or chunk.block_meta.get("chapter_id") != chapter_id:
            raise ValueError("SemanticChunk.block_meta.chapter_id must equal Chapter.id")
        chunk_id = _uuid4(UUID(str(chunk.uuid)), "SemanticChunk.uuid")
        if chunk_id in seen_uuids:
            raise ValueError("each chunk must have a distinct UUID4")
        seen_uuids.add(chunk_id)
        if chunk.ordinal != index or chunk.block_index != index:
            raise ValueError("chunk order metadata must match stable input order")
        if chunk.start is None or chunk.end is None or chunk.end <= chunk.start:
            raise ValueError("chunk range must satisfy start < end")
        if chunk.start < previous_end:
            raise ValueError("chunk ranges must be ordered and non-overlapping")
        previous_end = chunk.end

    return validated


def serialize_semantic_chunk(chunk: SemanticChunk) -> dict[str, Any]:
    """Serialize an adapter chunk using its public Pydantic serializer."""

    return validate_semantic_chunk(chunk).model_dump(mode="json")


def deserialize_semantic_chunk(payload: SemanticChunkPayload) -> SemanticChunk:
    """Deserialize a payload through the adapter's public factory."""

    if not isinstance(payload, Mapping):
        raise TypeError("chunk payload must be a mapping")
    return SemanticChunk.from_dict_with_autofill_and_validation(dict(payload))


def serialize_semantic_chunks(chunks: Sequence[SemanticChunk]) -> tuple[dict[str, Any], ...]:
    """Serialize an ordered collection without changing its order."""

    return tuple(serialize_semantic_chunk(chunk) for chunk in chunks)


__all__ = (
    "ChunkSpec",
    "SemanticChunk",
    "SemanticChunkPayload",
    "build_semantic_chunks",
    "deserialize_semantic_chunk",
    "serialize_semantic_chunk",
    "serialize_semantic_chunks",
    "validate_semantic_chunk",
    "validate_semantic_chunks",
)
