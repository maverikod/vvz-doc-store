from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from chunk_metadata_adapter import BlockType, SemanticChunk

from doc_store_server.ingestion.runtime_boundary import (
    InMemoryRuntimeStatus,
    RuntimeIngestionBoundary,
    RuntimeChunk,
    SEMANTIC_CLASSIFIER_DEFAULTS,
    _PersistencePlan,
    _chunk_features,
    _clear_reprocessing_flags,
    _insert_semantic_chunk_default_metrics,
    _mark_existing_hierarchy_deleted,
    _sentence_chunks_from_paragraph_batch,
    _sentence_chunks_for_paragraph,
    _semantic_classifier_values,
)


def _run(awaitable):
    return asyncio.run(awaitable)


def test_runtime_boundary_reports_database_unconfigured_without_crashing() -> None:
    status = InMemoryRuntimeStatus()
    boundary = RuntimeIngestionBoundary(None, status)

    result = _run(boundary(
        document_id="550e8400-e29b-41d4-a716-446655440000",
        source_version_id="source-v1",
        operation_id="550e8400-e29b-41d4-a716-446655440001",
        command="document_create",
        chunking_strategy="paragraph",
        raw_text="runtime source",
    ))

    assert result["status"] == "rolled_back"
    assert result["failure"]["code"] == "DATABASE_NOT_CONFIGURED"
    snapshot = status.get_status("550e8400-e29b-41d4-a716-446655440001")
    assert snapshot["status"] == "failed"
    assert snapshot["failure"]["code"] == "DATABASE_NOT_CONFIGURED"
    assert status.snapshot()["state"] == "idle"
    assert status.snapshot()["last_activity"]["status"] == "failed"


def test_runtime_status_uses_persisted_fallback_when_process_snapshot_is_missing(
    monkeypatch,
) -> None:
    status = InMemoryRuntimeStatus()

    def fake_lookup(
        self: InMemoryRuntimeStatus, operation_id: str, document_id: str | None
    ) -> dict[str, object]:
        assert operation_id == "550e8400-e29b-41d4-a716-446655440001"
        assert document_id == "550e8400-e29b-41d4-a716-446655440000"
        return {
            "status": "completed",
            "progress": 100,
            "document_id": document_id,
            "document_reference": {"id": document_id, "source_version": 7},
            "version_reference": {"document_id": document_id, "source_version": 7},
            "failure": None,
        }

    monkeypatch.setattr(InMemoryRuntimeStatus, "_lookup_persisted_status", fake_lookup)

    snapshot = status.get_status(
        "550e8400-e29b-41d4-a716-446655440001",
        "550e8400-e29b-41d4-a716-446655440000",
    )

    assert snapshot["status"] == "completed"
    assert snapshot["progress"] == 100
    assert snapshot["failure"] is None


def test_runtime_chunk_features_use_per_classifier_default_category_when_missing() -> None:
    plain = _chunk_features(
        "Plain paragraph without explicit classifier properties.",
        source_name="plain.txt",
        chunking_strategy="paragraph",
    )
    heading = _chunk_features("# Heading", source_name="heading.md", chunking_strategy="paragraph")

    assert plain["category"] == "uncategorized"
    assert "category:uncategorized" in plain["tags"]
    assert heading["category"] == "heading"


def test_runtime_default_chunk_status_marks_processing_required() -> None:
    assert SEMANTIC_CLASSIFIER_DEFAULTS["status"] == "needs_review"


def test_chunk_metadata_adapter_accepts_sentence_block_type() -> None:
    chunk = SemanticChunk.from_dict_with_autofill_and_validation(
        {"type": "DocBlock", "body": "Single sentence.", "block_type": "sentence"}
    )

    assert BlockType.from_string("sentence") == BlockType.SENTENCE
    assert chunk.block_type == BlockType.SENTENCE


def test_sentence_classifier_values_use_sentence_block_type() -> None:
    values = _semantic_classifier_values({"block_type": "sentence"})

    assert values["block_type"] == "sentence"


def test_runtime_ingestion_requests_paragraph_and_sentence_chunks(monkeypatch) -> None:
    document_id = uuid4()
    operation_id = uuid4()
    paragraph = RuntimeChunk(
        uuid=uuid4(),
        text="First sentence. Second sentence.",
        start=0,
        end=32,
        ordinal=0,
        metadata={},
    )
    sentences = (
        RuntimeChunk(uuid=uuid4(), text="First sentence.", start=0, end=15, ordinal=0, metadata={}),
        RuntimeChunk(uuid=uuid4(), text="Second sentence.", start=16, end=32, ordinal=1, metadata={}),
    )

    class FakeChunker:
        def __init__(self) -> None:
            self.calls: list[object] = []

        async def chunk(self, *, text: str, strategy: str, source_id: str) -> tuple[RuntimeChunk, ...]:
            assert text == "First sentence. Second sentence."
            assert source_id == str(document_id)
            self.calls.append(strategy)
            return (paragraph,)

        async def chunk_batch(
            self,
            *,
            texts: list[str],
            strategy: str,
            source_ids: list[str],
        ) -> tuple[tuple[RuntimeChunk, ...], ...]:
            assert texts == ["First sentence. Second sentence."]
            assert source_ids == [str(document_id)]
            self.calls.append((strategy, tuple(texts)))
            return (sentences,)

    fake = FakeChunker()
    boundary = RuntimeIngestionBoundary("postgresql://unused", InMemoryRuntimeStatus(), fake)

    def fake_prepare(**_: object) -> _PersistencePlan:
        return _PersistencePlan(
            document_id=document_id,
            source_version_id="source-v1",
            normalized_source_version_id="source-v1",
            operation_id=str(operation_id),
            command="document_create",
            text_value="First sentence. Second sentence.",
            filename=None,
            content_sha256="0" * 64,
            chunking_strategy="paragraph",
            source_version=1,
            title="First sentence.",
            source_name=None,
            length=32,
            body_sha256="1" * 64,
            file_id=uuid4(),
            doc_meta={},
        )

    def fake_persist(
        prepared: _PersistencePlan,
        paragraph_chunks: tuple[RuntimeChunk, ...],
        sentence_chunks: tuple[RuntimeChunk, ...],
    ) -> dict[str, object]:
        assert prepared.document_id == document_id
        assert paragraph_chunks == (paragraph,)
        assert sentence_chunks == sentences
        return {"document_id": str(document_id), "chunk_ids": tuple(str(item.uuid) for item in sentence_chunks)}

    monkeypatch.setattr(boundary, "_prepare_persistence", fake_prepare)
    monkeypatch.setattr(boundary, "_persist_prepared_chunks", fake_persist)

    result = _run(boundary._persist_source(
        requested_document_id=document_id,
        source_version_id="source-v1",
        normalized_source_version_id="source-v1",
        operation_id=str(operation_id),
        command="document_create",
        text_value="First sentence. Second sentence.",
        filename=None,
        content_sha256="0" * 64,
        chunking_strategy="paragraph",
    ))

    assert fake.calls == ["paragraph", ("sentence", ("First sentence. Second sentence.",))]
    assert result["chunk_ids"] == tuple(str(item.uuid) for item in sentences)


def test_sentence_chunks_are_selected_by_paragraph_ranges() -> None:
    paragraph = RuntimeChunk(uuid=uuid4(), text="Paragraph", start=10, end=40, ordinal=0, metadata={})
    before = RuntimeChunk(uuid=uuid4(), text="before", start=0, end=8, ordinal=0, metadata={})
    first = RuntimeChunk(uuid=uuid4(), text="first", start=10, end=20, ordinal=1, metadata={})
    second = RuntimeChunk(uuid=uuid4(), text="second", start=25, end=39, ordinal=2, metadata={})

    assert _sentence_chunks_for_paragraph(paragraph, (second, before, first)) == (first, second)


def test_sentence_batch_offsets_are_mapped_to_document_ranges() -> None:
    paragraph = RuntimeChunk(
        uuid=uuid4(),
        text="First sentence. Second.",
        start=100,
        end=123,
        ordinal=0,
        metadata={},
    )
    local_sentence = RuntimeChunk(
        uuid=uuid4(),
        text="Second.",
        start=16,
        end=23,
        ordinal=0,
        metadata={},
    )

    class FakeChunker:
        async def chunk_batch(self, **kwargs: object) -> tuple[tuple[RuntimeChunk, ...], ...]:
            assert kwargs["texts"] == ["First sentence. Second."]
            return ((local_sentence,),)

    result = _run(
        _sentence_chunks_from_paragraph_batch(
            chunker=FakeChunker(),
            paragraph_chunks=(paragraph,),
            source_id="550e8400-e29b-41d4-a716-446655440000",
        )
    )

    assert [(item.start, item.end, item.ordinal) for item in result] == [(116, 123, 0)]


def test_runtime_default_metrics_preserve_quality_for_later_worker() -> None:
    class RecordingConnection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def execute(self, statement: object, params: dict[str, object]) -> None:
            self.calls.append((str(statement), params))

    connection = RecordingConnection()
    chunk_id = "550e8400-e29b-41d4-a716-446655440003"

    _insert_semantic_chunk_default_metrics(connection, chunk_id)  # type: ignore[arg-type]

    metrics_sql, metrics_params = connection.calls[0]
    feedback_sql, feedback_params = connection.calls[1]
    assert "INSERT INTO semantic_chunk_metrics" in metrics_sql
    assert "quality_score, coverage, cohesion, boundary_prev" in metrics_sql
    assert "NULL, NULL, NULL, NULL, NULL, 0, FALSE, FALSE, FALSE" in metrics_sql
    assert metrics_params == {"chunk_uuid": chunk_id}
    assert "INSERT INTO semantic_chunk_feedback" in feedback_sql
    assert "VALUES (:chunk_uuid, 0, 0, 0)" in feedback_sql
    assert feedback_params == {"chunk_uuid": chunk_id}


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[object]:
        return self._rows

    def __iter__(self) -> object:
        return iter(self._rows)


class _MappingResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> "_MappingResult":
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _RecordingSqlConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: object, params: dict[str, object]) -> object:
        sql = str(statement)
        self.calls.append((sql, params))
        if "SELECT id FROM semantic_chunks" in sql:
            return _ScalarResult(["550e8400-e29b-41d4-a716-446655440010"])
        if "SELECT id, text FROM semantic_chunks" in sql:
            return _MappingResult(
                [{"id": "550e8400-e29b-41d4-a716-446655440010", "text": "chunk text"}]
            )
        return _ScalarResult([])


def test_runtime_checksum_rechunk_marks_existing_chunks_deleted_in_batch() -> None:
    connection = _RecordingSqlConnection()

    _mark_existing_hierarchy_deleted(connection, "550e8400-e29b-41d4-a716-446655440000")  # type: ignore[arg-type]

    sql = "\n".join(call[0] for call in connection.calls)
    assert "UPDATE semantic_chunk_embeddings SET active = FALSE" in sql
    assert "UPDATE semantic_chunks SET is_deleted = TRUE, deleted_at = now()" in sql
    assert "UPDATE paragraphs SET is_deleted = TRUE, deleted_at = now()" in sql
    assert "UPDATE chapters SET is_deleted = TRUE, deleted_at = now()" in sql
    assert "UPDATE chapters SET order_index = order_index +" in sql


def test_runtime_clear_reprocessing_flags_belongs_to_vectorizer_completion() -> None:
    connection = _RecordingSqlConnection()

    _clear_reprocessing_flags(connection, "550e8400-e29b-41d4-a716-446655440000")  # type: ignore[arg-type]

    sql = "\n".join(call[0] for call in connection.calls)
    assert "UPDATE documents SET needs_revectorize = FALSE" in sql
    assert "UPDATE files SET needs_revectorize = FALSE, needs_rechunk = FALSE" in sql


def test_runtime_boundary_writes_separated_error_and_text_logs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DOC_STORE_EVENT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DOC_STORE_TEXT_LOG_PREVIEW_CHARS", "7")
    status = InMemoryRuntimeStatus()
    boundary = RuntimeIngestionBoundary(None, status)

    _run(boundary(
        document_id="550e8400-e29b-41d4-a716-446655440000",
        source_version_id="source-v1",
        operation_id="550e8400-e29b-41d4-a716-446655440001",
        command="document_create",
        chunking_strategy="paragraph",
        raw_text="runtime source",
    ))

    text_events = [
        json.loads(line)
        for line in (tmp_path / "processed_texts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    error_events = [
        json.loads(line)
        for line in (tmp_path / "errors.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert text_events[0]["preview"] == "runtime"
    assert text_events[0]["preview_chars"] == 7
    assert error_events[0]["failure"]["code"] == "DATABASE_NOT_CONFIGURED"


def test_runtime_boundary_writes_processed_file_log_without_full_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DOC_STORE_EVENT_LOG_DIR", str(tmp_path))
    status = InMemoryRuntimeStatus()
    boundary = RuntimeIngestionBoundary(None, status)

    _run(boundary(
        document_id="550e8400-e29b-41d4-a716-446655440000",
        source_version_id="source-file-v1",
        operation_id="550e8400-e29b-41d4-a716-446655440002",
        command="document_create",
        chunking_strategy="paragraph",
        transferred_file={
            "filename": "sample.md",
            "media_type": "text/markdown",
            "content": "# Sample\n\nfile source",
        },
    ))

    file_events = [
        json.loads(line)
        for line in (tmp_path / "processed_files.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    text_events = [
        json.loads(line)
        for line in (tmp_path / "processed_texts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert file_events[0]["filename"] == "sample.md"
    assert file_events[0]["transferred_file"] == {
        "filename": "sample.md",
        "media_type": "text/markdown",
        "content_redacted": True,
    }
    assert "content" not in file_events[0]["transferred_file"]
    assert text_events[0]["preview"].startswith("# Sample")
