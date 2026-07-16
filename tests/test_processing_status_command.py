"""Contract tests for the read-only processing status command."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from doc_store_server.commands.processing_status_command import ProcessingStatusCommand
from mcp_proxy_adapter.core.errors import ValidationError


OPERATION_ID = "operation-123"
DOCUMENT_ID = "550e8400-e29b-41d4-a716-446655440000"


class FakeRuntimeStatusBoundary:
    """Record one status lookup and expose no orchestration capabilities."""

    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str | None]] = []

    def get_status(self, operation_id: str, document_id: str | None = None) -> dict[str, Any]:
        self.calls.append((operation_id, document_id))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def run(command: ProcessingStatusCommand, **params: Any) -> Any:
    return asyncio.run(command.execute(**params))


@pytest.mark.parametrize("status", ProcessingStatusCommand.status_vocabulary)
def test_projects_every_published_state_and_required_operation_identity(status: str) -> None:
    boundary = FakeRuntimeStatusBoundary({"status": status})

    result = run(
        ProcessingStatusCommand(),
        operation_id=OPERATION_ID,
        context={"runtime_status_boundary": boundary},
    )

    assert result.success is True
    assert result.data == {
        "operation_id": OPERATION_ID,
        "status": status,
        "progress": None,
        "timestamps": {},
        "document_reference": None,
        "version_reference": None,
        "failure": None,
    }
    assert boundary.calls == [(OPERATION_ID, None)]


def test_supports_optional_document_correlation_and_serializes_snapshot_fields() -> None:
    snapshot = {
        "status": "running",
        "document_id": DOCUMENT_ID,
        "progress": {"completed": 3, "total": 10},
        "timestamps": {
            "created_at": "2026-07-15T00:00:00Z",
            "updated_at": "2026-07-15T00:01:00Z",
        },
        "document_reference": {"id": DOCUMENT_ID, "version": 7},
        "version_reference": "document-version-7",
    }
    boundary = FakeRuntimeStatusBoundary(snapshot)

    result = run(
        ProcessingStatusCommand(),
        operation_id=OPERATION_ID,
        document_id=DOCUMENT_ID,
        context={"ingestion_runtime_status": boundary},
    )

    assert result.success is True
    assert result.data == {
        **snapshot,
        "operation_id": OPERATION_ID,
        "requested_document_id": DOCUMENT_ID,
        "failure": None,
    }
    assert boundary.calls == [(OPERATION_ID, DOCUMENT_ID)]


def test_completed_status_preserves_canonical_document_and_version_references() -> None:
    boundary = FakeRuntimeStatusBoundary(
        {
            "status": "completed",
            "document_id": DOCUMENT_ID,
            "progress": {"completed": 10, "total": 10},
            "timestamps": {"completed_at": "2026-07-15T00:02:00Z"},
            "document_reference": {"id": DOCUMENT_ID},
            "version_reference": {"document_id": DOCUMENT_ID, "version": 4},
        }
    )

    result = run(
        ProcessingStatusCommand(),
        operation_id=OPERATION_ID,
        document_id=DOCUMENT_ID,
        context={"runtime_status_boundary": boundary},
    )

    assert result.data["status"] == "completed"
    assert result.data["document_reference"] == {"id": DOCUMENT_ID}
    assert result.data["version_reference"] == {"document_id": DOCUMENT_ID, "version": 4}


def test_failed_boundary_returns_structured_diagnostics_without_rethrowing() -> None:
    boundary = FakeRuntimeStatusBoundary(RuntimeError("boundary unavailable"))

    result = run(
        ProcessingStatusCommand(),
        operation_id=OPERATION_ID,
        document_id=DOCUMENT_ID,
        context={"runtime_status_boundary": boundary},
    )

    assert result.success is False
    assert result.error == "boundary unavailable"
    assert result.data == {
        "operation_id": OPERATION_ID,
        "status": "failed",
        "progress": None,
        "timestamps": {},
        "document_reference": None,
        "version_reference": None,
        "failure": {
            "code": "STATUS_LOOKUP_FAILED",
            "message": "boundary unavailable",
            "type": "RuntimeError",
        },
        "requested_document_id": DOCUMENT_ID,
    }


def test_rejects_unknown_and_inconsistent_parameters() -> None:
    with pytest.raises(ValidationError):
        ProcessingStatusCommand().validate_params(
            {"operation_id": OPERATION_ID, "unexpected": True}
        )

    boundary = FakeRuntimeStatusBoundary({"status": "running", "document_id": "other"})
    result = run(
        ProcessingStatusCommand(),
        operation_id=OPERATION_ID,
        document_id=DOCUMENT_ID,
        context={"runtime_status_boundary": boundary},
    )

    assert result.success is False
    assert result.data["failure"]["code"] == "INVALID_PARAMS"
    assert "does not match requested document_id" in result.data["failure"]["message"]


def test_schema_and_metadata_are_complete_and_declare_read_only_contract() -> None:
    schema = ProcessingStatusCommand.get_schema()
    assert schema == {
        "type": "object",
        "properties": {
            "operation_id": {
                "type": "string",
                "description": "Required ingestion operation identifier.",
            },
            "document_id": {
                "type": "string",
                "description": "Optional canonical document correlation.",
            },
        },
        "required": ["operation_id"],
        "additionalProperties": False,
    }

    metadata = ProcessingStatusCommand.metadata()
    assert set(metadata) == {
        "name",
        "version",
        "description",
        "category",
        "author",
        "email",
        "detailed_description",
        "parameters",
        "return_value",
        "usage_examples",
        "error_cases",
        "best_practices",
    }
    assert metadata["name"] == "processing_status"
    assert metadata["version"] == "0.1.0"
    assert metadata["parameters"] == schema["properties"]
    assert "read-only" in metadata["detailed_description"]
    assert "poll" in metadata["detailed_description"]
    assert "retry" in metadata["detailed_description"]
    assert {"INVALID_PARAMS", "STATUS_LOOKUP_FAILED", "INVALID_STATUS"} <= set(
        metadata["error_cases"]
    )


def test_is_sync_read_only_and_performs_one_boundary_call_without_orchestration() -> None:
    boundary = FakeRuntimeStatusBoundary({"status": "pending"})
    command = ProcessingStatusCommand()

    result = run(
        command,
        operation_id=OPERATION_ID,
        context={"runtime_status_boundary": boundary},
    )

    assert command.use_queue is False
    assert boundary.calls == [(OPERATION_ID, None)]
    assert len(boundary.calls) == 1
    assert not hasattr(boundary, "poll")
    assert not hasattr(boundary, "retry")
    assert not hasattr(boundary, "cancel")
    assert not hasattr(boundary, "enqueue")
    assert not hasattr(boundary, "send_websocket")
    assert not hasattr(boundary, "query_database")
    assert not hasattr(boundary, "transport")
    assert result.success is True
