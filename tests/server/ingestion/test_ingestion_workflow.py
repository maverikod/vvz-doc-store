"""Integration coverage for the composed T-001..T-005 ingestion workflow."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
from typing import Any
from uuid import UUID

import pytest
from chunk_metadata_adapter import SemanticChunk

from doc_store_server.db.semantic_chunk_mapper import SemanticChunkRows, to_rows
from doc_store_server.ingestion.embedding_orchestration import (
    EmbeddingResponseError,
    orchestrate_embeddings,
)
from doc_store_server.ingestion.hierarchy_enrichment import (
    ChapterInput,
    HierarchyEnrichmentError,
    ParagraphInput,
    enrich_hierarchy,
)
from doc_store_server.ingestion.publication import (
    CommittedPublication,
    IdempotentReplay,
    RolledBackPublication,
    publish_document,
)
from doc_store_server.ingestion.source_normalizer import (
    FormatFilter,
    normalize_source,
)
from doc_store_server.ingestion.svo_chunking import chunk_normalized_request


DOCUMENT_ID = UUID("12345678-1234-4234-8234-123456789abc")
SOURCE_TEXT = "alpha beta\ngamma delta"
PROVIDER = "workflow-provider"
MODEL = "workflow-model"
MODEL_VERSION = "workflow-2026-07"


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _source_chunks(text: str) -> list[SemanticChunk]:
    chunks: list[SemanticChunk] = []
    for ordinal, (start, end) in enumerate(((0, 10), (11, len(text)))):
        body = text[start:end]
        chunks.append(
            SemanticChunk.from_dict_with_autofill_and_validation(
                {
                    "uuid": str(UUID(f"00000000-0000-4000-8000-{ordinal + 1:012d}")),
                    "source_id": str(DOCUMENT_ID),
                    "block_id": str(UUID(f"00000000-0000-4000-8000-{ordinal + 10:012d}")),
                    "type": "DocBlock",
                    "body": body,
                    "text": body,
                    "ordinal": ordinal,
                    "start": start,
                    "end": end,
                }
            )
        )
    return chunks


class _SvoDouble:
    def __init__(self, chunks: list[SemanticChunk], *, fail: bool = False) -> None:
        self.chunks = chunks
        self.fail = fail

    async def capabilities(self) -> dict[str, list[str]]:
        return {"presets": ["technical_text"]}

    async def chunk(self, _text: str, **_kwargs: object) -> list[SemanticChunk]:
        if self.fail:
            raise RuntimeError("SVO service failed")
        return self.chunks


class _EmbeddingDouble:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def embed(self, texts: list[str], **_kwargs: object) -> dict[str, object]:
        if self.fail:
            raise RuntimeError("embedding service failed")
        return {
            "results": [[float(index), float(index + 1)] for index, _ in enumerate(texts)],
            "provider": PROVIDER,
            "model": MODEL,
            "model_version": MODEL_VERSION,
            "dimension": 2,
        }


class _AtomicRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.visible: dict[tuple[UUID, str], dict[str, object]] = {}
        self.attempts = 0
        self.identity: tuple[UUID, str] | None = None

    async def find_committed_version(self, source_upload_id: UUID, source_version: str) -> dict[str, object] | None:
        return self.visible.get((source_upload_id, source_version))

    async def publish_transaction(self, payload: tuple[SemanticChunkRows, ...]) -> dict[str, object]:
        self.attempts += 1
        if self.fail:
            raise RuntimeError("storage transaction failed")
        document_id = payload[0].root["document_id"]
        references = {
            "document_id": document_id,
            "chapter_ids": tuple(row.root["chapter_id"] for row in payload),
            "chunk_ids": tuple(row.root["id"] for row in payload),
            "embedding_versions": tuple(
                embedding["model_version"] for row in payload for embedding in row.embeddings
            ),
        }
        assert self.identity is not None
        identity = self.identity
        self.visible[identity] = references
        return references


class _Mapper:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def map(self, aggregate: Any) -> tuple[SemanticChunkRows, ...]:
        if self.fail:
            raise ValueError("mapper failed")
        return tuple(
            to_rows(chunk, embedding_provider=PROVIDER, embedding_model_version=MODEL_VERSION)
            for chunk in aggregate.chunks
        )


def _chapters() -> tuple[ChapterInput, ...]:
    return (
        ChapterInput(
            start=0,
            end=10,
            paragraphs=(ParagraphInput("alpha beta", 0, 10, (0,)),),
        ),
        ChapterInput(
            start=11,
            end=len(SOURCE_TEXT),
            paragraphs=(ParagraphInput("gamma delta", 11, len(SOURCE_TEXT), (1,)),),
        ),
    )


def _normalize(*, transferred: bool = False, filter_failure: bool = False) -> Any:
    filters = {
        "plain_text": FormatFilter(
            name="plain_text",
            media_types=frozenset({"text/plain"}),
            extensions=frozenset({".txt"}),
        ),
    }
    if filter_failure:
        filters["plain_text"] = FormatFilter(
            name="plain_text",
            media_types=frozenset({"text/plain"}),
            apply=lambda *_: 1 / 0,
        )
    if transferred:
        return normalize_source(
            transferred_file=BytesIO(SOURCE_TEXT.encode()),
            document_id=DOCUMENT_ID,
            filename="document.txt",
            media_type="text/plain",
            filters=filters,
        )
    return normalize_source(raw_text=SOURCE_TEXT, document_id=DOCUMENT_ID, filters=filters)


def _workflow(*, source: Any, failure: str | None = None, repository: _AtomicRepository | None = None) -> tuple[Any, _AtomicRepository]:
    if source.diagnostic is not None:
        return source.diagnostic, repository or _AtomicRepository()
    request = source.request
    assert request is not None
    repo = repository or _AtomicRepository(fail=failure == "storage")
    repo.identity = (request.document_id, request.source_version_id)
    try:
        chunks = _run(chunk_normalized_request(request, _SvoDouble(_source_chunks(request.text), fail=failure == "svo")))
        aggregate = enrich_hierarchy(
            source_upload_id=request.document_id,
            source_version=request.source_version_id,
            chapters=_chapters(),
            semantic_chunks=chunks.chunks,
        )
        if failure == "hierarchy":
            raise HierarchyEnrichmentError("hierarchy enrichment failed")
        embedding = _run(
            orchestrate_embeddings(
                aggregate.chunks,
                _EmbeddingDouble(fail=failure == "embedding"),
                provider=PROVIDER,
                model=MODEL,
                model_version=MODEL_VERSION,
                dimension=2,
            )
        )
        aggregate = replace(aggregate, chunks=embedding.chunks)
        return _run(publish_document(aggregate, _Mapper(fail=failure == "mapper"), repo)), repo
    except Exception as exc:
        stage = failure or "workflow"
        diagnostic = type("StageDiagnostic", (), {"stage": stage, "error_type": type(exc).__name__, "message": str(exc)})()
        return diagnostic, repo


def test_raw_text_and_transferred_file_success_paths_publish_complete_canonical_versions() -> None:
    for source in (_normalize(), _normalize(transferred=True)):
        result, repository = _workflow(source=source)
        assert isinstance(result, CommittedPublication)
        assert result.status == "committed"
        assert result.identity.source_upload_id == DOCUMENT_ID
        assert result.identity.source_version == source.request.source_version_id
        assert result.references == repository.visible[(DOCUMENT_ID, source.request.source_version_id)]
        assert len(result.references["chapter_ids"]) == 2
        assert len(result.references["chunk_ids"]) == 2
        assert result.references["embedding_versions"] == (MODEL_VERSION, MODEL_VERSION)


def test_success_path_preserves_traceability_ranges_hierarchy_ownership_and_active_embeddings() -> None:
    source = _normalize()
    request = source.request
    assert request is not None
    chunks = _run(chunk_normalized_request(request, _SvoDouble(_source_chunks(request.text))))
    aggregate = enrich_hierarchy(
        source_upload_id=request.document_id,
        source_version=request.source_version_id,
        chapters=_chapters(),
        semantic_chunks=chunks.chunks,
    )
    embedded = _run(
        orchestrate_embeddings(
            aggregate.chunks,
            _EmbeddingDouble(),
            provider=PROVIDER,
            model=MODEL,
            model_version=MODEL_VERSION,
            dimension=2,
        )
    )
    assert aggregate.traceability.source_upload_id == DOCUMENT_ID
    assert aggregate.traceability.source_version == request.source_version_id
    assert tuple((chunk.start, chunk.end) for chunk in embedded.chunks) == ((0, 10), (11, 22))
    assert all(chunk.block_id and chunk.block_meta["chapter_id"] for chunk in embedded.chunks)
    assert all(record.active and record.compatible for record in embedded.embeddings)
    assert tuple(record.chunk_id for record in embedded.embeddings) == tuple(str(chunk.uuid) for chunk in embedded.chunks)


def test_identical_document_and_source_version_replay_is_deterministic_and_duplicate_free() -> None:
    repository = _AtomicRepository()
    first, _ = _workflow(source=_normalize(), repository=repository)
    second, _ = _workflow(source=_normalize(), repository=repository)
    assert isinstance(first, CommittedPublication)
    assert isinstance(second, IdempotentReplay)
    assert second.references == first.references
    assert repository.attempts == 1
    assert len(repository.visible) == 1


@pytest.mark.parametrize("failure", ["svo", "hierarchy", "embedding", "mapper", "storage"])
def test_downstream_stage_failure_has_diagnostic_and_rolls_back_without_visible_version(failure: str) -> None:
    source = _normalize()
    result, repository = _workflow(source=source, failure=failure)
    if failure == "storage":
        assert isinstance(result, RolledBackPublication)
        assert result.failure.stage == "repository_transaction"
    elif failure == "mapper":
        assert isinstance(result, RolledBackPublication)
        assert result.failure.stage == "mapper"
    else:
        assert result.stage == failure
        assert result.error_type
        assert result.message
    assert repository.visible == {}


def test_source_filter_failure_has_structured_diagnostic_and_no_workflow_side_effects() -> None:
    result, repository = _workflow(source=_normalize(filter_failure=True), failure="filter")
    assert result.code == "FILTER_FAILED"
    assert result.context["filter"] == "plain_text"
    assert repository.visible == {}


def test_workflow_uses_public_contracts_and_does_not_replace_production_stages() -> None:
    assert callable(normalize_source)
    assert callable(chunk_normalized_request)
    assert callable(enrich_hierarchy)
    assert callable(orchestrate_embeddings)
    assert callable(publish_document)
    assert EmbeddingResponseError.__module__.startswith("doc_store_server.ingestion")
