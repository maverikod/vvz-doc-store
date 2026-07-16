"""Contract tests for the document_rebind command."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
from mcp_proxy_adapter.core.errors import ValidationError

from doc_store_server.commands.document_rebind_command import DocumentRebindCommand
from doc_store_server.runtime.document_rebind import DocumentRebindError, DocumentRebindService


DOCUMENT_ID = "550e8400-e29b-41d4-a716-446655440000"
PROJECT_ID = "7254b86c-7456-47b3-8b7d-1590eef0f4a5"
EXISTING_PROJECT_ID = "a89f5a20-3661-4e50-b3c4-f86a5d7de16e"


class RecordingRebindBoundary:
    def __init__(self, outcome: Mapping[str, Any] | Exception | None = None) -> None:
        self.outcome = outcome or {
            "outcome": "rebound",
            "document_id": DOCUMENT_ID,
            "project": "doc-store",
            "updated": {"documents": 1, "semantic_chunks": 2},
        }
        self.calls: list[dict[str, Any]] = []

    def rebind_document(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return dict(self.outcome)


def test_document_rebind_delegates_project_and_properties() -> None:
    boundary = RecordingRebindBoundary()

    result = asyncio.run(
        DocumentRebindCommand().execute(
            context={"document_rebind_boundary": boundary},
            document_id=DOCUMENT_ID,
            project=" doc-store ",
            project_id=PROJECT_ID,
            project_description="runtime docs",
            document_properties={"owner": "docs"},
            chunk_properties={"scope": "runtime"},
        )
    )

    assert result.success is True
    assert result.data["outcome"] == "rebound"
    assert boundary.calls == [
        {
            "document_id": DOCUMENT_ID,
            "project": "doc-store",
            "project_id": PROJECT_ID,
            "project_description": "runtime docs",
            "document_properties": {"owner": "docs"},
            "chunk_properties": {"scope": "runtime"},
        }
    ]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"document_id": "not-a-uuid", "project": "doc-store"},
        {"document_id": DOCUMENT_ID, "project": "doc-store"},
        {"document_id": DOCUMENT_ID, "project": "doc-store", "project_id": PROJECT_ID},
        {"document_id": DOCUMENT_ID, "project_id": PROJECT_ID},
        {"document_id": DOCUMENT_ID},
        {"document_id": DOCUMENT_ID, "project": " "},
        {"document_id": DOCUMENT_ID, "chunk_properties": "not-an-object"},
        {
            "document_id": DOCUMENT_ID,
            "project": "doc-store",
            "project_id": PROJECT_ID,
            "project_description": "runtime docs",
            "unexpected": True,
        },
    ],
)
def test_document_rebind_rejects_invalid_params(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocumentRebindCommand().validate_params(params)


def test_document_rebind_maps_runtime_errors_to_structured_failure() -> None:
    boundary = RecordingRebindBoundary(
        DocumentRebindError(
            "DOCUMENT_NOT_FOUND",
            "document was not found",
            {"document_id": DOCUMENT_ID},
        )
    )

    result = asyncio.run(
        DocumentRebindCommand().execute(
            context={"document_rebind_boundary": boundary},
            document_id=DOCUMENT_ID,
            project="doc-store",
            project_id=PROJECT_ID,
            project_description="runtime docs",
        )
    )

    assert result.success is False
    assert result.data == {
        "outcome": "document_not_found",
        "document_id": DOCUMENT_ID,
        "details": {"document_id": DOCUMENT_ID},
    }


def test_document_rebind_service_reuses_existing_project_name(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _RecordingConnection()

    class FakeTransaction:
        def __enter__(self) -> _RecordingConnection:
            return connection

        def __exit__(self, *args: object) -> None:
            return None

    class FakeEngine:
        def begin(self) -> FakeTransaction:
            return FakeTransaction()

        def dispose(self) -> None:
            return None

    monkeypatch.setattr(
        "doc_store_server.runtime.document_rebind.create_engine",
        lambda *args, **kwargs: FakeEngine(),
    )

    result = DocumentRebindService("postgresql://example/db").rebind_document(
        document_id=DOCUMENT_ID,
        project="doc-store-runtime",
        project_id=PROJECT_ID,
        project_description="runtime docs",
        chunk_properties={"scope": "runtime"},
    )

    assert result["project_id"] == EXISTING_PROJECT_ID
    assert result["document_properties"]["project_id"] == EXISTING_PROJECT_ID
    assert result["chunk_properties"]["project_id"] == EXISTING_PROJECT_ID
    assert connection.insert_project_calls == 0
    assert connection.document_meta["project_id"] == EXISTING_PROJECT_ID
    assert connection.document_meta["project"] == "doc-store-runtime"


def test_document_rebind_schema_and_metadata_are_complete() -> None:
    schema = DocumentRebindCommand.get_schema()
    metadata = DocumentRebindCommand.metadata()

    assert set(schema["properties"]) == {
        "document_id",
        "project",
        "project_id",
        "project_description",
        "document_properties",
        "chunk_properties",
    }
    assert schema["required"] == ["document_id"]
    assert schema["additionalProperties"] is False
    assert set(metadata["parameters"]) == set(schema["properties"])
    assert "DOCUMENT_NOT_FOUND" in metadata["error_cases"]
    assert "embedding rows" in metadata["detailed_description"]


class _FakeResult:
    def __init__(self, rows: list[Mapping[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])

    def mappings(self) -> "_FakeResult":
        return self

    def first(self) -> Mapping[str, Any] | None:
        return self.rows[0] if self.rows else None

    def one_or_none(self) -> Mapping[str, Any] | None:
        return self.rows[0] if self.rows else None

    def one(self) -> Mapping[str, Any]:
        return self.rows[0]

    def all(self) -> list[Mapping[str, Any]]:
        return self.rows


class _RecordingConnection:
    def __init__(self) -> None:
        self.document_meta: dict[str, Any] = {}
        self.insert_project_calls = 0

    def execute(self, statement: Any, params: Mapping[str, Any] | None = None) -> _FakeResult:
        sql = str(statement)
        if "SELECT block_meta FROM documents" in sql:
            return _FakeResult([{"block_meta": {"source": "original"}}])
        if "UPDATE projects" in sql and "WHERE name = :name" in sql:
            return _FakeResult([{"id": EXISTING_PROJECT_ID}])
        if "INSERT INTO projects" in sql:
            self.insert_project_calls += 1
            return _FakeResult([{"id": params["project_id"] if params else PROJECT_ID}])
        if "UPDATE documents" in sql and params is not None:
            self.document_meta = json.loads(str(params["block_meta"]))
            return _FakeResult()
        if "SELECT id::text AS id, block_meta FROM" in sql:
            return _FakeResult()
        return _FakeResult()
