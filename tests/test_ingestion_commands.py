"""Focused contract tests for the public document ingestion commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from mcp_proxy_adapter.commands.result import ErrorResult, SuccessResult

from doc_store_server.commands.ingestion_commands import (
    DocumentChunkCommand,
    DocumentCreateCommand,
    DocumentUpdateCommand,
)


DOCUMENT_ID = "550e8400-e29b-41d4-a716-446655440000"
SOURCE_VERSION_ID = "source-v1"
COMMANDS = (DocumentCreateCommand, DocumentUpdateCommand)
INGESTION_PARAMS = {
    DocumentCreateCommand: {"chunking_strategy": "paragraph"},
    DocumentUpdateCommand: {},
}


class RecordingBoundary:
    """Fake G-006 boundary that records only the command delegation."""

    def __init__(self, outcome: Mapping[str, Any] | None = None) -> None:
        self.outcome = dict(outcome or {})
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        return dict(self.outcome)


class NoTransferIO(dict[str, str]):
    """A transfer reference whose I/O must never be touched by a command."""

    def read(self) -> bytes:
        raise AssertionError("command performed transfer I/O")


@pytest.mark.parametrize("command_class", COMMANDS)
@pytest.mark.parametrize(
    ("source", "expected_source"),
    [("raw_text", {"raw_text": "source text"}), ("transferred_file", {"transferred_file": NoTransferIO(transfer_id="x")})],
)
def test_command_delegates_exact_identity_and_source_to_g006(
    command_class: type[Any],
    source: str,
    expected_source: dict[str, Any],
) -> None:
    boundary = RecordingBoundary({"status": "accepted", "trace_id": "trace-1"})
    result = asyncio.run(
        command_class().execute(
            context={"ingestion_boundary": boundary},
            document_id=DOCUMENT_ID,
            source_version_id=SOURCE_VERSION_ID,
            **INGESTION_PARAMS[command_class],
            **expected_source,
        )
    )

    assert isinstance(result, SuccessResult)
    assert result.data["status"] == "accepted"
    assert result.data["document_id"] == DOCUMENT_ID
    assert result.data["source_version_id"] == SOURCE_VERSION_ID
    assert boundary.calls == [
        {
            "document_id": DOCUMENT_ID,
            "source_version_id": SOURCE_VERSION_ID,
            **INGESTION_PARAMS[command_class],
            source: expected_source[source],
            "operation_id": result.data["operation_id"],
            "command": command_class.name,
        }
    ]


@pytest.mark.parametrize("command_class", COMMANDS)
@pytest.mark.parametrize(
    "params",
    [
        {"raw_text": "text", "transferred_file": {"transfer_id": "x"}},
        {},
    ],
)
def test_command_rejects_raw_text_and_transferred_file_xor_violations(
    command_class: type[Any], params: dict[str, Any]
) -> None:
    result = asyncio.run(
        command_class().execute(
            context={"ingestion_boundary": RecordingBoundary()},
            document_id=DOCUMENT_ID,
            source_version_id=SOURCE_VERSION_ID,
            **INGESTION_PARAMS[command_class],
            **params,
        )
    )

    assert isinstance(result, ErrorResult)
    assert result.code == -32602
    assert result.details.get("code") == "INVALID_SOURCE_COUNT"


@pytest.mark.parametrize("command_class", COMMANDS)
def test_command_returns_structured_validation_errors_without_delegating(
    command_class: type[Any],
) -> None:
    boundary = RecordingBoundary()
    result = asyncio.run(
        command_class().execute(
            context={"ingestion_boundary": boundary},
            document_id="not-a-uuid",
            source_version_id="source-v1",
            **INGESTION_PARAMS[command_class],
            raw_text="text",
            unexpected="field",
        )
    )

    assert isinstance(result, ErrorResult)
    assert result.code == -32602
    assert result.details == {"fields": ["unexpected"]}
    assert boundary.calls == []


@pytest.mark.parametrize("command_class", COMMANDS)
def test_command_falls_back_to_installed_runtime_boundary(
    command_class: type[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOC_STORE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DOC_STORE_CONFIG", raising=False)

    result = asyncio.run(
        command_class().execute(
            document_id=DOCUMENT_ID,
            source_version_id=SOURCE_VERSION_ID,
            **(
                INGESTION_PARAMS[command_class]
                if command_class is DocumentCreateCommand
                else {"chunking_strategy": "paragraph"}
            ),
            raw_text="text",
        )
    )

    assert isinstance(result, SuccessResult)
    assert result.data["status"] == "failed"
    assert result.data["failure"]["code"] == "DATABASE_NOT_CONFIGURED"


@pytest.mark.parametrize("command_class", COMMANDS)
def test_command_uses_installed_runtime_boundary_when_context_omits_boundary(
    command_class: type[Any],
) -> None:
    boundary = RecordingBoundary({"status": "committed"})
    previous = command_class.ingestion_boundary
    command_class.ingestion_boundary = boundary
    try:
        result = asyncio.run(
            command_class().execute(
                document_id=DOCUMENT_ID,
                source_version_id=SOURCE_VERSION_ID,
                **INGESTION_PARAMS[command_class],
                raw_text="text",
            )
        )
    finally:
        command_class.ingestion_boundary = previous

    assert isinstance(result, SuccessResult)
    assert result.data["status"] == "completed"
    assert boundary.calls


@pytest.mark.parametrize("command_class", COMMANDS)
@pytest.mark.parametrize(
    ("boundary_status", "public_status"),
    [
        ("accepted", "accepted"),
        ("idempotent", "idempotent"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("committed", "completed"),
        ("idempotent_replay", "idempotent"),
        ("rolled_back", "failed"),
    ],
)
def test_command_maps_boundary_results_to_stable_public_statuses(
    command_class: type[Any], boundary_status: str, public_status: str
) -> None:
    result = asyncio.run(
        command_class().execute(
            context={"ingestion_boundary": RecordingBoundary({"status": boundary_status})},
            document_id=DOCUMENT_ID,
            source_version_id=SOURCE_VERSION_ID,
            **INGESTION_PARAMS[command_class],
            raw_text="text",
        )
    )

    assert isinstance(result, SuccessResult)
    assert result.data["status"] == public_status
    assert result.data["operation_id"]


@pytest.mark.parametrize("command_class", COMMANDS)
def test_command_maps_orchestration_exception_to_structured_failed_result(
    command_class: type[Any],
) -> None:
    def failing_boundary(**_kwargs: Any) -> None:
        raise RuntimeError("storage must remain below G-006")

    result = asyncio.run(
        command_class().execute(
            context={"ingestion_boundary": failing_boundary},
            document_id=DOCUMENT_ID,
            source_version_id=SOURCE_VERSION_ID,
            **INGESTION_PARAMS[command_class],
            raw_text="text",
        )
    )

    assert isinstance(result, SuccessResult)
    assert result.data == {
        "status": "failed",
        "operation_id": result.data["operation_id"],
        "document_id": DOCUMENT_ID,
        "source_version_id": SOURCE_VERSION_ID,
        "error": "RuntimeError",
        "message": "storage must remain below G-006",
    }


@pytest.mark.parametrize("command_class", COMMANDS)
def test_command_schema_and_metadata_are_complete_and_boundary_scoped(
    command_class: type[Any],
) -> None:
    schema = command_class.get_schema()
    metadata = command_class.metadata()

    assert set(schema) == {"type", "properties", "required", "additionalProperties", "x-oneOf", "x-use-queue"}
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {
        "document_id",
        "source_version_id",
        "raw_text",
        "transferred_file",
        "chunking_strategy",
    }
    expected_required = ["document_id", "source_version_id"]
    if command_class is DocumentCreateCommand:
        expected_required.append("chunking_strategy")
    assert schema["required"] == expected_required
    assert schema["additionalProperties"] is False
    assert schema["x-oneOf"] == ["raw_text", "transferred_file"]
    assert schema["x-use-queue"] is True

    assert set(metadata) == {
        "name", "version", "description", "category", "author", "email",
        "detailed_description", "parameters", "return_value", "error_cases",
        "usage_examples", "best_practices",
    }
    assert metadata["name"] == command_class.name
    assert set(metadata["parameters"]) == set(schema["properties"])
    assert all(isinstance(value, str) and value for value in metadata["parameters"].values())
    assert {"INVALID_SOURCE_COUNT", "INVALID_PARAMS", "INGESTION_BOUNDARY_UNAVAILABLE"} <= set(metadata["error_cases"])
    description = metadata["detailed_description"]
    for forbidden_stage in ("transfer", "queue", "WebSocket", "chunking", "embedding", "persistence"):
        assert forbidden_stage in description
    assert "G-006" in description


def test_document_create_requires_chunking_strategy_before_delegating() -> None:
    boundary = RecordingBoundary()

    result = asyncio.run(
        DocumentCreateCommand().execute(
            context={"ingestion_boundary": boundary},
            document_id=DOCUMENT_ID,
            source_version_id=SOURCE_VERSION_ID,
            raw_text="source text",
        )
    )

    assert isinstance(result, ErrorResult)
    assert result.details == {"missing_parameters": ["chunking_strategy"]}
    assert boundary.calls == []


def test_document_chunk_delegates_document_id_and_optional_strategy() -> None:
    boundary = RecordingBoundary({"status": "committed", "source_version_id": "source-v2"})

    result = asyncio.run(
        DocumentChunkCommand().execute(
            context={"ingestion_boundary": boundary},
            document_id=DOCUMENT_ID,
            chunking_strategy="sentence",
        )
    )

    assert isinstance(result, SuccessResult)
    assert result.data["status"] == "completed"
    assert result.data["source_version_id"] == "source-v2"
    assert boundary.calls == [
        {
            "document_id": DOCUMENT_ID,
            "chunking_strategy": "sentence",
            "source_version_id": "stored",
            "operation_id": result.data["operation_id"],
            "command": "document_chunk",
        }
    ]


def test_document_chunk_schema_uses_stored_strategy_by_default() -> None:
    schema = DocumentChunkCommand.get_schema()
    metadata = DocumentChunkCommand.metadata()

    assert set(schema["properties"]) == {"document_id", "chunking_strategy"}
    assert schema["required"] == ["document_id"]
    assert "x-oneOf" not in schema
    assert set(metadata["parameters"]) == {"document_id", "chunking_strategy"}
