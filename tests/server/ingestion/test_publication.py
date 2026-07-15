"""Focused contract tests for the canonical publication boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from chunk_metadata_adapter import SemanticChunk

import doc_store_server.ingestion.publication as publication_module
from doc_store_server.db.semantic_chunk_mapper import SemanticChunkRows, to_rows
from doc_store_server.ingestion.hierarchy_enrichment import (
    ChapterInput,
    ParagraphInput,
    enrich_hierarchy,
)
from doc_store_server.ingestion.publication import (
    CommittedPublication,
    IdempotentReplay,
    RolledBackPublication,
    publish_document,
)


SOURCE_UPLOAD_ID = uuid4()
SOURCE_VERSION = "content-version-7"


def _aggregate() -> object:
    source_chunks = tuple(
        SemanticChunk.from_dict_with_autofill_and_validation(
            {
                "uuid": str(uuid4()),
                "source_id": str(uuid4()),
                "block_id": str(uuid4()),
                "type": "DocBlock",
                "body": body,
                "text": body,
                "ordinal": index,
                "start": start,
                "end": end,
                "block_meta": {"chapter_id": str(uuid4()), "accepted": True},
            }
        )
        for index, (body, start, end) in enumerate(
            (("first", 0, 5), ("chapter one", 5, 16), ("second", 16, 22), ("third", 22, 30))
        )
    )
    aggregate = enrich_hierarchy(
        source_upload_id=SOURCE_UPLOAD_ID,
        source_version=SOURCE_VERSION,
        chapters=(
            ChapterInput(
                start=0,
                end=22,
                paragraphs=(
                    ParagraphInput("paragraph one", 0, 16, (0, 1)),
                    ParagraphInput("paragraph two", 16, 22, (2,)),
                ),
            ),
            ChapterInput(
                start=22,
                end=30,
                paragraphs=(ParagraphInput("paragraph three", 22, 30, (3,)),),
            ),
        ),
        semantic_chunks=source_chunks,
    )
    enriched_chunks = tuple(
        chunk.model_copy(
            update={
                "embedding": [float(index), float(index + 1)],
                "embedding_model": "embedding-model",
                "tags": ["canonical", f"chunk-{index}"],
            }
        )
        for index, chunk in enumerate(aggregate.chunks)
    )
    return replace(aggregate, chunks=enriched_chunks)


class ActualMapperDouble:
    """Use the accepted G-005 mapper for each public chunk, without storage."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def map(self, aggregate: Any) -> tuple[SemanticChunkRows, ...]:
        self.calls += 1
        if self.fail:
            raise ValueError("mapper root phase failed")
        return tuple(
            to_rows(
                chunk,
                embedding_provider="embedding-provider",
                embedding_model_version="embedding-version",
            )
            for chunk in aggregate.chunks
        )


class AtomicRepositoryDouble:
    """A transaction double whose staged writes become visible only at commit."""

    def __init__(self, *, failure_phase: str | None = None) -> None:
        self.failure_phase = failure_phase
        self.lookup_calls: list[tuple[UUID, str | int]] = []
        self.transaction_calls = 0
        self.visible_references: dict[tuple[UUID, str | int], dict[str, object]] = {}

    async def find_committed_version(
        self, source_upload_id: UUID, source_version: str | int
    ) -> dict[str, object] | None:
        identity = (source_upload_id, source_version)
        self.lookup_calls.append(identity)
        return self.visible_references.get(identity)

    async def publish_transaction(
        self, payload: tuple[SemanticChunkRows, ...]
    ) -> dict[str, object]:
        self.transaction_calls += 1
        identity = (SOURCE_UPLOAD_ID, SOURCE_VERSION)
        staged: dict[str, object] = {}
        if self.failure_phase == "root":
            raise RuntimeError("repository root phase failed")
        staged["document_id"] = payload[0].root["document_id"]
        for index, rows in enumerate(payload):
            if self.failure_phase == f"child-{index}":
                raise RuntimeError(f"repository child phase {index} failed")
            assert rows.root["document_id"] == staged["document_id"]
        references = {
            "document_id": staged["document_id"],
            "chapter_ids": tuple(rows.root["chapter_id"] for rows in payload),
            "embedding_versions": tuple(
                row["model_version"] for rows in payload for row in rows.embeddings
            ),
        }
        self.visible_references[identity] = references
        return references


def _run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def test_publication_commits_complete_aggregate_and_versioned_embeddings() -> None:
    aggregate = _aggregate()
    mapper = ActualMapperDouble()
    repository = AtomicRepositoryDouble()

    result = _run(publish_document(aggregate, mapper, repository))

    assert isinstance(result, CommittedPublication)
    assert result.status == "committed"
    assert result.identity.source_upload_id == SOURCE_UPLOAD_ID
    assert result.identity.source_version == SOURCE_VERSION
    assert result.canonical_version_refs == repository.visible_references[(SOURCE_UPLOAD_ID, SOURCE_VERSION)]
    assert result.canonical_version_refs["document_id"] == aggregate.document.id
    assert len(aggregate.chapters) == 2
    assert len(aggregate.paragraphs) == 3
    assert len(aggregate.chunks) == 4
    assert all(type(chunk) is SemanticChunk for chunk in aggregate.chunks)
    assert result.canonical_version_refs["embedding_versions"] == ("embedding-version",) * 4
    assert repository.lookup_calls == [(SOURCE_UPLOAD_ID, SOURCE_VERSION)]
    assert mapper.calls == 1
    assert repository.transaction_calls == 1


def test_publication_resolves_identity_before_mapping_and_replays_without_mutation() -> None:
    aggregate = _aggregate()
    mapper = ActualMapperDouble()
    repository = AtomicRepositoryDouble()

    first = _run(publish_document(aggregate, mapper, repository))
    second = _run(publish_document(aggregate, mapper, repository))

    assert isinstance(first, CommittedPublication)
    assert isinstance(second, IdempotentReplay)
    assert second.status == "idempotent_replay"
    assert second.references == first.references
    assert mapper.calls == 1
    assert repository.transaction_calls == 1
    assert repository.lookup_calls == [(SOURCE_UPLOAD_ID, SOURCE_VERSION)] * 2


@pytest.mark.parametrize("failure", ["mapper", "repository-root", "repository-child"])
def test_publication_returns_typed_rollback_without_partial_visibility(failure: str) -> None:
    aggregate = _aggregate()
    mapper = ActualMapperDouble(fail=failure == "mapper")
    repository = AtomicRepositoryDouble(
        failure_phase="root" if failure == "repository-root" else "child-2" if failure == "repository-child" else None
    )

    result = _run(publish_document(aggregate, mapper, repository))

    assert isinstance(result, RolledBackPublication)
    assert result.status == "rolled_back"
    assert result.references is None
    assert (SOURCE_UPLOAD_ID, SOURCE_VERSION) not in repository.visible_references
    assert result.failure.error_type in {"ValueError", "RuntimeError"}
    assert result.failure.message
    if failure == "mapper":
        assert result.failure.stage == "mapper"
        assert repository.transaction_calls == 0
    else:
        assert result.failure.stage == "repository_transaction"
        assert repository.transaction_calls == 1


def test_publication_boundary_does_not_define_persistence_or_diagnostic_implementation() -> None:
    forbidden = {
        "Base",
        "AsyncSession",
        "DocumentRepository",
        "SemanticChunkRepository",
        "create_engine",
        "engine",
        "SQLAlchemy",
        "text",
        "to_rows",
        "from_rows",
        "reconstruct",
        "diagnose",
        "final_diagnostics",
    }
    assert not forbidden.intersection(vars(publication_module))
    assert publication_module.publish is publication_module.publish_document

