"""Integration contract for the complete doc-store application command surface."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from doc_store_server.commands import registration
from doc_store_server.commands.chunk_query_search_command import ChunkQuerySearchCommand
from doc_store_server.commands.document_delete_command import DocumentDeleteCommand
from doc_store_server.commands.ingestion_commands import (
    DocumentCreateCommand,
    DocumentUpdateCommand,
)
from doc_store_server.commands.processing_status_command import ProcessingStatusCommand
from doc_store_server.commands.retrieval_commands import (
    ChapterGetCommand,
    DocumentGetCommand,
    ParagraphGetCommand,
)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_COMMANDS = {
    "document_get": (DocumentGetCommand, "sync"),
    "chapter_get": (ChapterGetCommand, "sync"),
    "paragraph_get": (ParagraphGetCommand, "sync"),
    "document_create": (DocumentCreateCommand, "queue"),
    "document_update": (DocumentUpdateCommand, "queue"),
    "processing_status": (ProcessingStatusCommand, "sync"),
    "document_delete": (DocumentDeleteCommand, "sync"),
    "chunk_query_search": (ChunkQuerySearchCommand, "sync"),
}
EXPECTED_METADATA = {
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


class RecordingRegistry:
    """Adapter-facing registry double for the single application hook."""

    def __init__(self) -> None:
        self.calls: list[tuple[type[Any], str]] = []

    def register(self, command_class: type[Any], command_type: str) -> None:
        self.calls.append((command_class, command_type))


def _application_commands(registry: Any) -> dict[str, type[Any]]:
    commands = getattr(registry, "_commands")
    command_types = getattr(registry, "_command_types")
    return {
        name: command
        for name, command in commands.items()
        if command_types.get(name) == "custom"
    }


def test_manifest_registry_and_live_help_are_one_exact_surface() -> None:
    """The manifest, registry, and generated help must describe one command set."""

    assert {entry.command_name for entry in registration.DOC_STORE_COMMAND_MANIFEST} == set(
        EXPECTED_COMMANDS
    )
    assert len(registration.DOC_STORE_COMMAND_MANIFEST) == len(EXPECTED_COMMANDS)

    recording = RecordingRegistry()
    registration.register_doc_store_commands(recording)
    assert recording.calls == [(command, "custom") for command, _ in EXPECTED_COMMANDS.values()]

    from mcp_proxy_adapter.commands.command_registry import CommandRegistry
    from doc_store_server.main import initialize_command_registry

    live = CommandRegistry()
    initialize_command_registry(live)
    commands = _application_commands(live)
    assert set(commands) == set(EXPECTED_COMMANDS)
    assert len(commands) == len(set(commands))

    for entry in registration.DOC_STORE_COMMAND_MANIFEST:
        command, expected_mode = EXPECTED_COMMANDS[entry.command_name]
        live_command = commands[entry.command_name]
        assert live_command is command
        assert command.__module__ == entry.import_module
        assert command.name == entry.command_name
        assert ("queue" if command.use_queue else "sync") == expected_mode
        assert entry.execution_mode == expected_mode
        assert entry.metadata_identity == f"{command.__name__}.metadata"
        assert entry.schema_identity == f"{command.__name__}.get_schema"

        metadata = command.metadata()
        schema = command.get_schema()
        assert set(metadata) == EXPECTED_METADATA
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        assert set(schema["required"]) <= set(schema["properties"])
        assert schema["additionalProperties"] is False
        assert metadata["name"] == command.name
        assert metadata["parameters"]
        assert metadata["return_value"]
        assert metadata["error_cases"]

        help_entry = live.get_command_info(entry.command_name)
        assert help_entry is not None
        assert help_entry["schema"] == schema
        assert help_entry["metadata"]["name"] == metadata["name"]
        for field in EXPECTED_METADATA - {"name"}:
            assert help_entry["metadata"][field] == metadata[field]
        assert help_entry["ai_metadata"] == metadata


def test_application_modules_are_importable_without_competing_infrastructure() -> None:
    """The app owns contracts and boundaries, while transport stays adapter-owned."""

    for entry in registration.DOC_STORE_COMMAND_MANIFEST:
        module = __import__(entry.import_module, fromlist=[entry.command_class.__name__])
        assert getattr(module, entry.command_class.__name__) is entry.command_class

    source_files = tuple((ROOT / "src/doc_store_server").rglob("*.py"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    imports = []
    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports.extend(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        imports.extend(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )

    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imports)
    assert not any(token in source for token in ("FastAPI(", "APIRouter(", "@app.route", "@app.get", "@app.post"))
    assert "register_custom_commands_hook" in source
    defined_names = {
        node.name
        for path in source_files
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not any(
        any(term in name.lower() for term in ("websocket", "authenticate", "rest"))
        for name in defined_names
    )
    registration_path = ROOT / "src/doc_store_server/commands/registration.py"
    assert sum(
        1
        for node in ast.walk(ast.parse(registration_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register"
    ) == 1
    assert not (ROOT / "src/doc_store_server/query/grammar.lark").exists()
    assert not any("QuerySpec" in path.read_text(encoding="utf-8") for path in source_files)


class FakeRetrieval:
    async def get_document(self, document_id: Any, source_version: int | None = None) -> Any:
        if str(document_id).endswith("0002"):
            raise LookupError("missing document")
        return {"document_id": str(document_id), "source_version": source_version}

    async def get_chapter(self, chapter_id: Any, source_version: int | None = None) -> Any:
        return {"chapter_id": str(chapter_id), "source_version": source_version}

    async def get_paragraph(self, paragraph_id: Any, source_version: int | None = None) -> Any:
        return {"paragraph_id": str(paragraph_id), "source_version": source_version}


class FakeStatus:
    def get_status(self, operation_id: str, document_id: str | None = None) -> dict[str, Any]:
        return {"status": "completed", "operation_id": operation_id, "document_id": document_id}


class FakeDelete:
    def delete_document(self, document_id: str, version_token: str) -> dict[str, str]:
        return {"outcome": "deleted", "document_id": document_id, "version_token": version_token}


@pytest.mark.parametrize(
    ("command_class", "params", "context"),
    [
        (DocumentGetCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001"}, {"retrieval_boundary": FakeRetrieval()}),
        (ChapterGetCommand, {"chapter_id": "550e8400-e29b-41d4-a716-446655440003"}, {"retrieval_boundary": FakeRetrieval()}),
        (ParagraphGetCommand, {"paragraph_id": "550e8400-e29b-41d4-a716-446655440004"}, {"retrieval_boundary": FakeRetrieval()}),
        (DocumentCreateCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001", "source_version_id": "v1", "raw_text": "hello"}, {"ingestion_boundary": lambda **_: {"status": "committed"}}),
        (DocumentUpdateCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001", "source_version_id": "v2", "raw_text": "hello"}, {"ingestion_boundary": lambda **_: {"status": "committed"}}),
        (ProcessingStatusCommand, {"operation_id": "op-1"}, {"runtime_status_boundary": FakeStatus()}),
        (DocumentDeleteCommand, {"document_id": "doc-1", "version_token": "v1"}, {"canonical_document_service": FakeDelete()}),
        (ChunkQuerySearchCommand, {"query": {"search_query": "hello"}}, {"search_orchestrator": lambda *_args, **_kwargs: {"status": "success", "data": {"results": []}}}),
    ],
)
def test_every_registered_command_exercises_a_representative_success_path(
    command_class: type[Any], params: dict[str, Any], context: dict[str, Any]
) -> None:
    result = asyncio.run(command_class().execute(**params, context=context))
    assert getattr(result, "success", True) is True
    assert getattr(result, "data", None) is not None


def test_representative_failure_paths_keep_stable_error_contracts() -> None:
    retrieval = asyncio.run(
        DocumentGetCommand().execute(
            document_id="550e8400-e29b-41d4-a716-446655440002",
            context={"retrieval_boundary": FakeRetrieval()},
        )
    )
    assert retrieval.success is False
    assert retrieval.error.startswith("NOT_FOUND:")

    status = asyncio.run(
        ProcessingStatusCommand().execute("op-1", context={"runtime_status_boundary": None})
    )
    assert status.success is False
    assert status.error == "Ingestion runtime-status boundary is unavailable."

    search = ChunkQuerySearchCommand()
    with pytest.raises(Exception, match="legacy filter_expr"):
        search.validate_params({"query": {"filter_expr": "status = 'indexed'"}})
