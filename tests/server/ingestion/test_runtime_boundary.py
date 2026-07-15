from __future__ import annotations

import json

from doc_store_server.ingestion.runtime_boundary import (
    InMemoryRuntimeStatus,
    RuntimeIngestionBoundary,
    _chunk_units,
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
