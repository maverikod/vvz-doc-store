from __future__ import annotations

from doc_store_server.ingestion.runtime_boundary import (
    InMemoryRuntimeStatus,
    RuntimeIngestionBoundary,
)


def test_runtime_boundary_reports_database_unconfigured_without_crashing() -> None:
    status = InMemoryRuntimeStatus()
    boundary = RuntimeIngestionBoundary(None, status)

    result = boundary(
        document_id="550e8400-e29b-41d4-a716-446655440000",
        source_version_id="source-v1",
        operation_id="550e8400-e29b-41d4-a716-446655440001",
        command="document_create",
        raw_text="runtime source",
    )

    assert result["status"] == "rolled_back"
    assert result["failure"]["code"] == "DATABASE_NOT_CONFIGURED"
    snapshot = status.get_status("550e8400-e29b-41d4-a716-446655440001")
    assert snapshot["status"] == "failed"
    assert snapshot["failure"]["code"] == "DATABASE_NOT_CONFIGURED"
