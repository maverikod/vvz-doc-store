from __future__ import annotations

import json

from doc_store_server.ingestion.runtime_boundary import (
    InMemoryRuntimeStatus,
    RuntimeIngestionBoundary,
    _chunk_features,
    _chunk_units,
    _insert_semantic_chunk_default_metrics,
    _paragraphs,
)


def test_runtime_boundary_reports_database_unconfigured_without_crashing() -> None:
    status = InMemoryRuntimeStatus()
    boundary = RuntimeIngestionBoundary(None, status)

    result = boundary(
        document_id="550e8400-e29b-41d4-a716-446655440000",
        source_version_id="source-v1",
        operation_id="550e8400-e29b-41d4-a716-446655440001",
        command="document_create",
        chunking_strategy="paragraph",
        raw_text="runtime source",
    )

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


def test_runtime_paragraph_split_preserves_blank_line_order() -> None:
    text = (
        "# Title\n\n"
        "First paragraph mentions PostgreSQL indexes.\n\n"
        "Second paragraph explains semantic retrieval.\n\n"
        "Third paragraph carries project doc-store and tag api."
    )

    paragraphs = _paragraphs(text)

    assert [paragraph for paragraph, _, _ in paragraphs] == [
        "# Title",
        "First paragraph mentions PostgreSQL indexes.",
        "Second paragraph explains semantic retrieval.",
        "Third paragraph carries project doc-store and tag api.",
    ]
    assert [text[start:end] for _, start, end in paragraphs] == [
        "# Title",
        "First paragraph mentions PostgreSQL indexes.",
        "Second paragraph explains semantic retrieval.",
        "Third paragraph carries project doc-store and tag api.",
    ]


def test_runtime_chunk_units_support_document_strategies() -> None:
    text = "First sentence. Second sentence!\n\nThird paragraph keeps semantic context."

    paragraph_units = _chunk_units(text, "paragraph")
    sentence_units = _chunk_units(text, "sentence")
    semantic_units = _chunk_units(text, "semantic")

    assert [unit[0] for unit in paragraph_units] == [
        "First sentence. Second sentence!",
        "Third paragraph keeps semantic context.",
    ]
    assert [unit[0] for unit in sentence_units] == [
        "First sentence.",
        "Second sentence!",
        "Third paragraph keeps semantic context.",
    ]
    assert [unit[0] for unit in semantic_units] == [
        "First sentence. Second sentence!\n\nThird paragraph keeps semantic context."
    ]


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


def test_runtime_boundary_writes_separated_error_and_text_logs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DOC_STORE_EVENT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DOC_STORE_TEXT_LOG_PREVIEW_CHARS", "7")
    status = InMemoryRuntimeStatus()
    boundary = RuntimeIngestionBoundary(None, status)

    boundary(
        document_id="550e8400-e29b-41d4-a716-446655440000",
        source_version_id="source-v1",
        operation_id="550e8400-e29b-41d4-a716-446655440001",
        command="document_create",
        chunking_strategy="paragraph",
        raw_text="runtime source",
    )

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

    boundary(
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
    )

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
