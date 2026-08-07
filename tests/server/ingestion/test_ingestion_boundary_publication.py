"""Caller-invariance evidence for the published ingestion boundary.

The boundary was split into ``PublicationMapper``, ``PublicationRepository`` and
an orchestrator over ``publish_document``.  Nothing outside the ingestion package
was allowed to notice: ``main.py`` still wires one ``RuntimeIngestionBoundary``
into the three ingestion commands, ``commands/ingestion_commands.py`` still
resolves it through ``installed_ingestion_boundary``, and
``commands/health_command.py`` still reads its worker metrics from
``installed_runtime_status``.  These tests hold that surface still, then show the
designed pipeline is what actually runs behind it.

No database is required: the repository's two storage-touching inner methods are
replaced, so the real repository class, the real mapper and the real
``publish_document`` all stay on the path while nothing connects.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

from doc_store_server import main
from doc_store_server.commands import health_command
from doc_store_server.commands.health_command import DocStoreHealthCommand
from doc_store_server.commands.ingestion_commands import (
    DocumentChunkCommand,
    DocumentCreateCommand,
    DocumentUpdateCommand,
)
from doc_store_server.commands.processing_status_command import ProcessingStatusCommand
from doc_store_server.ingestion import publication_repository as repository_module
from doc_store_server.ingestion import runtime_boundary as boundary_module
from doc_store_server.ingestion.hierarchy_enrichment import CanonicalIngestionAggregate
from doc_store_server.ingestion.persistence_plan import PersistencePlan
from doc_store_server.ingestion.publication import publish_document
from doc_store_server.ingestion.publication_mapper import (
    NormalizedRequestContext,
    PublicationMapper,
)
from doc_store_server.ingestion.publication_repository import PublicationRepository
from doc_store_server.ingestion.runtime_boundary import (
    InMemoryRuntimeStatus,
    RuntimeIngestionBoundary,
    installed_ingestion_boundary,
    installed_runtime_status,
)
from doc_store_server.ingestion.svo_chunking import RuntimeChunk


DATABASE_URL = "postgresql://ingestion-boundary-publication/unused"
DOCUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
OPERATION_ID = "55555555-5555-4555-8555-555555555555"
SOURCE_VERSION_ID = "source-v1"
SOURCE_TEXT = "First sentence. Second sentence."
PARAGRAPH_ID = UUID("22222222-2222-4222-8222-222222222222")
FIRST_SENTENCE_ID = UUID("33333333-3333-4333-8333-333333333333")
SECOND_SENTENCE_ID = UUID("44444444-4444-4444-8444-444444444444")

COMMITTED_REFERENCES: dict[str, Any] = {
    "document_id": str(DOCUMENT_ID),
    "source_version_id": SOURCE_VERSION_ID,
    "source_version": 7,
    "chunk_ids": (str(FIRST_SENTENCE_ID), str(SECOND_SENTENCE_ID)),
    "chunking_strategy": "paragraph",
}
REPLAY_REFERENCES: dict[str, Any] = {
    **COMMITTED_REFERENCES,
    "idempotent": True,
    "idempotent_reason": "source_version_match",
}

# Every class attribute ``main.configure_runtime_boundaries`` assigns, so the
# wiring test can exercise the real function without leaking installed
# boundaries into the rest of the session.
WIRING_ATTRIBUTES = (
    "ingestion_boundary",
    "export_boundary",
    "reconstruction_boundary",
    "document_service",
    "retrieval_boundary",
    "runtime_status_boundary",
    "search_orchestrator",
    "vectorization_boundary",
    "chunk_version_boundary",
    "lifecycle_boundary",
    "runtime_config",
)


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class FakeChunker:
    """Produce one paragraph and its two sentences without an SVO service."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def chunk(
        self, *, text: str, strategy: str, source_id: str
    ) -> tuple[RuntimeChunk, ...]:
        self.calls.append((strategy, text, source_id))
        return (
            RuntimeChunk(
                uuid=PARAGRAPH_ID,
                text=text,
                start=0,
                end=len(text),
                ordinal=0,
                metadata={},
            ),
        )

    async def chunk_batch(
        self, *, texts: list[str], strategy: str, source_ids: list[str]
    ) -> tuple[tuple[RuntimeChunk, ...], ...]:
        self.calls.append((strategy, tuple(texts), tuple(source_ids)))
        return tuple(
            (
                RuntimeChunk(
                    uuid=FIRST_SENTENCE_ID,
                    text="First sentence.",
                    start=0,
                    end=15,
                    ordinal=0,
                    metadata={},
                ),
                RuntimeChunk(
                    uuid=SECOND_SENTENCE_ID,
                    text="Second sentence.",
                    start=16,
                    end=len(value),
                    ordinal=1,
                    metadata={},
                ),
            )
            for value in texts
        )


def _boundary(chunker: FakeChunker | None = None) -> tuple[RuntimeIngestionBoundary, InMemoryRuntimeStatus]:
    status = InMemoryRuntimeStatus()
    return RuntimeIngestionBoundary(DATABASE_URL, status, chunker or FakeChunker()), status


def _create_request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "document_id": str(DOCUMENT_ID),
        "source_version_id": SOURCE_VERSION_ID,
        "operation_id": OPERATION_ID,
        "command": "document_create",
        "chunking_strategy": "paragraph",
        "raw_text": SOURCE_TEXT,
    }
    request.update(overrides)
    return request


# ----------------------------------------------------------------------
# Stubs for the mapper/repository seam
# ----------------------------------------------------------------------


def _stub_plan(context: NormalizedRequestContext) -> PersistencePlan:
    return PersistencePlan(
        document_id=context.requested_document_id,
        source_version_id=context.source_version_id,
        normalized_source_version_id=context.normalized_source_version_id,
        operation_id=context.operation_id,
        command=context.command,
        text_value=context.text_value,
        filename=context.filename,
        content_sha256=context.content_sha256,
        chunking_strategy=context.chunking_strategy,
        source_version=COMMITTED_REFERENCES["source_version"],
        title="First sentence.",
        source_name=None,
        length=len(context.text_value),
        body_sha256="b" * 64,
        file_id=UUID("66666666-6666-4666-8666-666666666666"),
        doc_meta={"stubbed": True},
    )


class StubMapper:
    """A mapper with the production signature and a fixed plan."""

    instances: list["StubMapper"] = []

    def __init__(self, **request: Any) -> None:
        self.request = dict(request)
        self.context = NormalizedRequestContext(**request)
        self.map_calls = 0
        StubMapper.instances.append(self)

    def map(self, aggregate: CanonicalIngestionAggregate) -> PersistencePlan:
        self.map_calls += 1
        return _stub_plan(self.context)


def _stub_repository_class(
    *, existing: dict[str, Any] | None = None, failure: Exception | None = None
) -> type:
    """A repository double constructed exactly like the production one."""

    class StubRepository:
        instances: list["StubRepository"] = []

        def __init__(self, database_url: str, *, chunker: Any = None) -> None:
            self.database_url = database_url
            self.chunker = chunker
            self.published: list[PersistencePlan] = []
            self.lookups: list[tuple[UUID, str | int]] = []
            StubRepository.instances.append(self)

        async def find_committed_version(
            self,
            source_upload_id: UUID,
            source_version: str | int,
            *,
            plan: PersistencePlan | None = None,
        ) -> dict[str, Any] | None:
            self.lookups.append((source_upload_id, source_version))
            return dict(existing) if existing is not None else None

        async def publish_transaction(
            self,
            payload: PersistencePlan,
            paragraph_chunks: Any = None,
            sentence_chunks: Any = None,
        ) -> dict[str, Any]:
            self.published.append(payload)
            if failure is not None:
                raise failure
            return dict(COMMITTED_REFERENCES)

    return StubRepository


def _install_stub_seam(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: dict[str, Any] | None = None,
    failure: Exception | None = None,
) -> type:
    StubMapper.instances.clear()
    monkeypatch.setattr(boundary_module, "PublicationMapper", StubMapper)
    repository = _stub_repository_class(existing=existing, failure=failure)
    monkeypatch.setattr(repository_module, "PublicationRepository", repository)
    return repository


def _install_storage_free_repository(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: dict[str, Any] | None = None,
    failure: Exception | None = None,
) -> dict[str, Any]:
    """Keep the real repository class, remove only the two calls that hit storage."""

    captured: dict[str, Any] = {}

    def find_committed_version(
        self: PublicationRepository,
        source_upload_id: UUID,
        source_version: str | int,
        plan: PersistencePlan | None,
    ) -> dict[str, Any] | None:
        captured["lookup"] = (source_upload_id, source_version, plan)
        return dict(existing) if existing is not None else None

    def publish(
        self: PublicationRepository,
        payload: PersistencePlan,
        paragraph_chunks: Any,
        sentence_chunks: Any,
    ) -> dict[str, Any]:
        captured["plan"] = payload
        captured["paragraph_chunks"] = tuple(paragraph_chunks)
        captured["sentence_chunks"] = tuple(sentence_chunks)
        if failure is not None:
            raise failure
        return {
            "document_id": str(payload.document_id),
            "source_version_id": payload.source_version_id,
            "source_version": payload.source_version,
            "chunk_ids": tuple(str(chunk.uuid) for chunk in sentence_chunks),
            "chunking_strategy": payload.chunking_strategy,
        }

    monkeypatch.setattr(
        PublicationRepository, "_find_committed_version", find_committed_version
    )
    monkeypatch.setattr(PublicationRepository, "_publish", publish)
    return captured


def _spy_on_publish_document(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any, Any]]:
    """Record every ``publish_document`` call while still performing it."""

    calls: list[tuple[Any, Any, Any]] = []

    async def spy(aggregate: Any, mapper: Any, repository: Any) -> Any:
        calls.append((aggregate, mapper, repository))
        return await publish_document(aggregate, mapper, repository)

    monkeypatch.setattr(boundary_module, "publish_document", spy)
    return calls


# ----------------------------------------------------------------------
# 1. The caller surface
# ----------------------------------------------------------------------


def test_installed_boundary_and_status_still_wire_the_three_commands_and_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main.py, the ingestion commands and the health command see no change."""

    chunker = FakeChunker()
    status = InMemoryRuntimeStatus()
    monkeypatch.setattr(boundary_module, "_INSTALLED_STATUS", status)
    monkeypatch.setattr(boundary_module, "_INSTALLED_BOUNDARY", None)
    monkeypatch.setattr(boundary_module, "_INSTALLED_BOUNDARY_URL", None)
    monkeypatch.setattr(
        boundary_module, "installed_svo_runtime_chunker", lambda *_args: chunker
    )
    monkeypatch.setenv("DOC_STORE_DATABASE_URL", DATABASE_URL)

    # commands/ingestion_commands.py resolves the boundary through this one
    # installer, and it still returns a RuntimeIngestionBoundary bound to the
    # process-local status the health command reads.
    assert installed_runtime_status() is status
    installed = installed_ingestion_boundary()
    assert isinstance(installed, RuntimeIngestionBoundary)
    assert installed_ingestion_boundary() is installed

    # main.py wires the very same class into the three ingestion commands.
    for target in vars(main).values():
        if not isinstance(target, type):
            continue
        for attribute in WIRING_ATTRIBUTES:
            if hasattr(target, attribute):
                monkeypatch.setattr(target, attribute, getattr(target, attribute))
    sentinel = object()
    monkeypatch.setattr(main, "installed_svo_runtime_chunker", lambda _config: chunker)
    for installer in (
        "installed_search_orchestrator",
        "installed_retrieval_boundary",
        "installed_entity_lifecycle_service",
        "installed_document_export_service",
        "installed_text_reconstruction_service",
        "installed_document_service",
        "installed_vectorization_service",
        "installed_chunk_text_version_service",
    ):
        monkeypatch.setattr(main, installer, lambda _config: sentinel)

    main.configure_runtime_boundaries({"database": {"url": DATABASE_URL}})

    wired = DocumentCreateCommand.ingestion_boundary
    assert isinstance(wired, RuntimeIngestionBoundary)
    assert DocumentUpdateCommand.ingestion_boundary is wired
    assert DocumentChunkCommand.ingestion_boundary is wired
    assert ProcessingStatusCommand.runtime_status_boundary is status

    # commands/health_command.py still reports the metrics that status holds.
    monkeypatch.setattr(
        health_command,
        "installed_vectorization_snapshot",
        lambda _config: {"state": "idle", "current_activity": None, "last_activity": None},
    )
    status.record(
        OPERATION_ID,
        {
            "status": "running",
            "document_id": str(DOCUMENT_ID),
            "worker": {"action": "persisting_hierarchy_and_embeddings"},
        },
    )

    health = _run(DocStoreHealthCommand().execute())

    worker = health.data["components"]["worker"]
    assert worker["state"] == "running"
    assert worker["current_activity"]["operation_id"] == OPERATION_ID
    assert worker["current_activity"]["action"] == "persisting_hierarchy_and_embeddings"


# ----------------------------------------------------------------------
# 2. The returned dictionary keys and status values, per outcome
# ----------------------------------------------------------------------


def test_boundary_call_returns_the_same_keys_and_statuses_on_every_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed, idempotent replay and failure keep their public shape."""

    # Committed.
    repository = _install_stub_seam(monkeypatch)
    boundary, status = _boundary()
    committed = _run(boundary(**_create_request()))

    assert committed == {"status": "committed", **COMMITTED_REFERENCES}
    assert StubMapper.instances[0].map_calls >= 1
    assert repository.instances[0].database_url == DATABASE_URL
    assert repository.instances[0].published[0].document_id == DOCUMENT_ID
    snapshot = status.get_status(OPERATION_ID)
    assert snapshot["status"] == "completed"
    assert snapshot["progress"] == 100
    assert snapshot["failure"] is None
    assert snapshot["document_reference"] == {
        "id": str(DOCUMENT_ID),
        "version": COMMITTED_REFERENCES["source_version"],
    }
    assert snapshot["version_reference"] == COMMITTED_REFERENCES

    # Idempotent replay: the lookup answers, so nothing is published.
    monkeypatch.undo()
    repository = _install_stub_seam(monkeypatch, existing=REPLAY_REFERENCES)
    boundary, status = _boundary()
    replay = _run(boundary(**_create_request()))

    assert replay == {"status": "idempotent_replay", **REPLAY_REFERENCES}
    assert set(replay) - {"status"} >= set(COMMITTED_REFERENCES)
    assert repository.instances[0].published == []
    assert status.get_status(OPERATION_ID)["status"] == "idempotent"

    # Failure: a rolled-back publication is one controlled failure record.
    monkeypatch.undo()
    _install_stub_seam(monkeypatch, failure=RuntimeError("storage transaction failed"))
    boundary, status = _boundary()
    rolled_back = _run(boundary(**_create_request()))

    assert rolled_back == {
        "status": "rolled_back",
        "failure": {
            "code": "PERSISTENCE_FAILED",
            "message": "storage transaction failed",
            "type": "RuntimeError",
        },
    }
    failed = status.get_status(OPERATION_ID)
    assert failed["status"] == "failed"
    assert failed["document_reference"] is None
    assert failed["version_reference"] is None
    assert failed["failure"]["code"] == "PERSISTENCE_FAILED"


# ----------------------------------------------------------------------
# 3. The designed pipeline is what produces those outcomes
# ----------------------------------------------------------------------


def test_publish_document_with_the_real_mapper_and_repository_is_on_the_production_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outcomes above come from publish_document over the two protocols."""

    # The bodies these tests used to reach into are gone, not merely bypassed.
    assert not hasattr(boundary_module, "_PersistencePlan")
    assert not hasattr(RuntimeIngestionBoundary, "_prepare_persistence")
    assert not hasattr(RuntimeIngestionBoundary, "_persist_prepared_chunks")

    captured = _install_storage_free_repository(monkeypatch)
    calls = _spy_on_publish_document(monkeypatch)
    chunker = FakeChunker()
    boundary, _status = _boundary(chunker)

    committed = _run(boundary(**_create_request()))

    assert committed["status"] == "committed"
    assert len(calls) == 1
    aggregate, mapper, repository = calls[0]
    assert isinstance(aggregate, CanonicalIngestionAggregate)
    assert aggregate.traceability.source_upload_id == DOCUMENT_ID
    assert aggregate.traceability.source_version == SOURCE_VERSION_ID
    assert isinstance(mapper, PublicationMapper)
    assert isinstance(repository.repository, PublicationRepository)

    # The plan written is exactly the one the real mapper maps for this request.
    expected = PublicationMapper(
        requested_document_id=DOCUMENT_ID,
        source_version_id=SOURCE_VERSION_ID,
        normalized_source_version_id=mapper.context.normalized_source_version_id,
        operation_id=OPERATION_ID,
        command="document_create",
        text_value=SOURCE_TEXT,
        filename=None,
        content_sha256=mapper.context.content_sha256,
        chunking_strategy="paragraph",
        media_type=mapper.context.media_type,
        byte_length=mapper.context.byte_length,
    ).map(aggregate)
    assert captured["plan"] == expected

    # And the identity lookup that decides a replay runs before any mapping.
    lookup_document_id, lookup_version, _plan = captured["lookup"]
    assert lookup_document_id == DOCUMENT_ID
    assert lookup_version == SOURCE_VERSION_ID

    # The same composition answers a replay without publishing anything.
    monkeypatch.undo()
    replay_captured = _install_storage_free_repository(
        monkeypatch, existing=REPLAY_REFERENCES
    )
    replay_calls = _spy_on_publish_document(monkeypatch)
    boundary, _status = _boundary()

    replay = _run(boundary(**_create_request()))

    assert replay == {"status": "idempotent_replay", **REPLAY_REFERENCES}
    assert len(replay_calls) == 1
    assert "plan" not in replay_captured


# ----------------------------------------------------------------------
# 4. One end-to-end normalize, chunk, map and publish pass
# ----------------------------------------------------------------------


def test_end_to_end_normalize_chunk_map_and_publish_produces_the_expected_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole sequence runs, and the captured plan and references hold."""

    captured = _install_storage_free_repository(monkeypatch)
    chunker = FakeChunker()
    boundary, status = _boundary(chunker)

    result = _run(
        boundary(**_create_request(command="document_update", chunking_strategy="paragraph"))
    )

    # Normalize, then chunk paragraphs, then the sentences of those paragraphs.
    assert chunker.calls == [
        ("paragraph", SOURCE_TEXT, str(DOCUMENT_ID)),
        ("sentence", (SOURCE_TEXT,), (str(DOCUMENT_ID),)),
    ]
    assert captured["paragraph_chunks"] == (
        RuntimeChunk(
            uuid=PARAGRAPH_ID, text=SOURCE_TEXT, start=0, end=32, ordinal=0, metadata={}
        ),
    )
    assert tuple(chunk.uuid for chunk in captured["sentence_chunks"]) == (
        FIRST_SENTENCE_ID,
        SECOND_SENTENCE_ID,
    )

    plan = captured["plan"]
    assert isinstance(plan, PersistencePlan)
    assert plan.document_id == DOCUMENT_ID
    assert plan.source_version_id == SOURCE_VERSION_ID
    assert plan.operation_id == OPERATION_ID
    assert plan.command == "document_update"
    assert plan.text_value == SOURCE_TEXT
    assert plan.filename is None
    assert plan.source_name is None
    assert plan.chunking_strategy == "paragraph"
    assert plan.title == SOURCE_TEXT
    assert plan.length == len(SOURCE_TEXT)
    assert plan.media_type == "text/plain"
    assert plan.byte_length == len(SOURCE_TEXT.encode("utf-8"))
    # The normalizer's digest of the normalized bytes is the plan's identity, and
    # the three names the document stores it under never disagree.
    assert plan.content_sha256 == plan.body_sha256
    assert plan.doc_meta["source_sha256"] == plan.body_sha256
    assert plan.doc_meta["file_sha256"] == plan.body_sha256
    assert plan.doc_meta["body_sha256"] == plan.body_sha256
    assert plan.doc_meta["file_id"] == str(plan.file_id)
    assert plan.doc_meta["ingestion_command"] == "document_update"
    assert plan.doc_meta["operation_id"] == OPERATION_ID
    assert plan.doc_meta["source_version_id"] == SOURCE_VERSION_ID
    assert plan.doc_meta["normalized_source_version_id"] == plan.normalized_source_version_id
    assert plan.doc_meta["chunking_strategy"] == "paragraph"
    assert plan.doc_meta["checksum_algorithm"] == "sha256"

    assert result == {
        "status": "committed",
        "document_id": str(DOCUMENT_ID),
        "source_version_id": SOURCE_VERSION_ID,
        "source_version": plan.source_version,
        "chunk_ids": (str(FIRST_SENTENCE_ID), str(SECOND_SENTENCE_ID)),
        "chunking_strategy": "paragraph",
    }
    assert status.get_status(OPERATION_ID)["status"] == "completed"
