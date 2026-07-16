"""Integration contract for the complete doc-store application command surface."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from doc_store_server.commands import registration
from doc_store_server.commands.chunk_query_search_command import ChunkQuerySearchCommand
from doc_store_server.commands.corpus_audit_command import CorpusAuditCommand
from doc_store_server.commands.document_delete_command import DocumentDeleteCommand
from doc_store_server.commands.document_export_command import DocumentExportCommand
from doc_store_server.commands.document_rebind_command import DocumentRebindCommand
from doc_store_server.commands.entity_lifecycle_commands import (
    EntityCreateCommand,
    EntityGetCommand,
    EntityHardDeleteCommand,
    EntityListCommand,
    EntityReferencesCommand,
    EntitySoftDeleteCommand,
    EntityUpdateCommand,
    EntityUndeleteCommand,
)
from doc_store_server.commands.health_command import DocStoreHealthCommand
from doc_store_server.commands.info import InfoCommand
from doc_store_server.commands.ingestion_commands import (
    DocumentChunkCommand,
    DocumentCreateCommand,
    DocumentUpdateCommand,
)
from doc_store_server.commands.processing_status_command import ProcessingStatusCommand
from doc_store_server.commands.retrieval_commands import (
    ChapterGetCommand,
    DocumentGetCommand,
    ParagraphGetByNumberCommand,
    ParagraphGetCommand,
)
from doc_store_server.commands.semantic_relations_command import SemanticRelationsCommand
from doc_store_server.commands.uuid4_command import Uuid4Command


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_COMMANDS = {
    "health": (DocStoreHealthCommand, "sync"),
    "info": (InfoCommand, "sync"),
    "uuid4": (Uuid4Command, "sync"),
    "document_get": (DocumentGetCommand, "sync"),
    "chapter_get": (ChapterGetCommand, "sync"),
    "paragraph_get": (ParagraphGetCommand, "sync"),
    "paragraph_get_by_number": (ParagraphGetByNumberCommand, "sync"),
    "document_create": (DocumentCreateCommand, "queue"),
    "document_update": (DocumentUpdateCommand, "queue"),
    "document_chunk": (DocumentChunkCommand, "queue"),
    "document_export": (DocumentExportCommand, "sync"),
    "document_rebind": (DocumentRebindCommand, "sync"),
    "processing_status": (ProcessingStatusCommand, "sync"),
    "document_delete": (DocumentDeleteCommand, "sync"),
    "entity_create": (EntityCreateCommand, "sync"),
    "entity_list": (EntityListCommand, "sync"),
    "entity_get": (EntityGetCommand, "sync"),
    "entity_update": (EntityUpdateCommand, "sync"),
    "entity_soft_delete": (EntitySoftDeleteCommand, "sync"),
    "entity_undelete": (EntityUndeleteCommand, "sync"),
    "entity_hard_delete": (EntityHardDeleteCommand, "sync"),
    "entity_references": (EntityReferencesCommand, "sync"),
    "chunk_query_search": (ChunkQuerySearchCommand, "sync"),
    "semantic_relations": (SemanticRelationsCommand, "sync"),
    "corpus_audit": (CorpusAuditCommand, "sync"),
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
        if command.name != "health":
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

    async def get_paragraph_by_number(
        self,
        document_id: Any,
        paragraph_number: int,
        source_version: int | None = None,
    ) -> Any:
        return {
            "document_id": str(document_id),
            "paragraph_number": paragraph_number,
            "source_version": source_version,
            "text": "paragraph text",
        }


class FakeStatus:
    def get_status(self, operation_id: str, document_id: str | None = None) -> dict[str, Any]:
        return {"status": "completed", "operation_id": operation_id, "document_id": document_id}


class FakeDelete:
    def delete_document(self, document_id: str, version_token: str) -> dict[str, str]:
        return {"outcome": "deleted", "document_id": document_id, "version_token": version_token}


class FakeRebind:
    def rebind_document(self, **kwargs: Any) -> dict[str, Any]:
        return {"outcome": "rebound", "document_id": kwargs["document_id"]}


class FakeExport:
    def export_document(self, **kwargs: Any) -> dict[str, Any]:
        return {"outcome": "exported", "document_id": kwargs["document_id"], "file_id": "550e8400-e29b-41d4-a716-446655440009"}


class FakeLifecycle:
    def create_entity(self, **kwargs: Any) -> dict[str, Any]:
        return {"entity_type": kwargs["entity_type"], "outcome": "created", "value": kwargs["values"]}

    def update_entity(self, **kwargs: Any) -> dict[str, Any]:
        return {"entity_type": kwargs["entity_type"], "outcome": "updated", "id": kwargs["entity_id"], "value": kwargs["values"]}

    def list_entities(self, **kwargs: Any) -> dict[str, Any]:
        return {"entity_type": kwargs["entity_type"], "items": [], "limit": 50, "offset": 0, "total": 0, "show_deleted": False}

    def get_entity(self, **kwargs: Any) -> dict[str, Any]:
        return {"entity_type": kwargs["entity_type"], "id": kwargs["entity_id"], "value": {"id": kwargs["entity_id"]}}

    def soft_delete(self, **kwargs: Any) -> dict[str, Any]:
        return {"outcome": "updated", "updated": {"documents": len(kwargs["ids"])}, "is_deleted": True}

    def undelete(self, **kwargs: Any) -> dict[str, Any]:
        return {"outcome": "updated", "updated": {"documents": len(kwargs["ids"])}, "is_deleted": False}

    def hard_delete(self, **kwargs: Any) -> dict[str, Any]:
        return {"outcome": "deleted", "deleted": {"documents": len(kwargs["ids"])}, "blocked": []}

    def references_for(self, **kwargs: Any) -> dict[str, Any]:
        return {"entity_type": kwargs["entity_type"], "id": kwargs["entity_id"], "references": []}


@pytest.mark.parametrize(
    ("command_class", "params", "context"),
    [
        (DocumentGetCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001"}, {"retrieval_boundary": FakeRetrieval()}),
        (ChapterGetCommand, {"chapter_id": "550e8400-e29b-41d4-a716-446655440003"}, {"retrieval_boundary": FakeRetrieval()}),
        (ParagraphGetCommand, {"paragraph_id": "550e8400-e29b-41d4-a716-446655440004"}, {"retrieval_boundary": FakeRetrieval()}),
        (ParagraphGetByNumberCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001", "paragraph_number": 2}, {"retrieval_boundary": FakeRetrieval()}),
        (DocumentCreateCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001", "source_version_id": "v1", "chunking_strategy": "paragraph", "raw_text": "hello"}, {"ingestion_boundary": lambda **_: {"status": "committed"}}),
        (DocumentUpdateCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001", "source_version_id": "v2", "raw_text": "hello"}, {"ingestion_boundary": lambda **_: {"status": "committed"}}),
        (DocumentChunkCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001"}, {"ingestion_boundary": lambda **_: {"status": "committed", "source_version_id": "v2"}}),
        (DocumentExportCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001", "path": "/tmp/doc.txt"}, {"document_export_boundary": FakeExport()}),
        (
            DocumentRebindCommand,
            {
                "document_id": "550e8400-e29b-41d4-a716-446655440001",
                "project": "doc-store",
                "project_id": "7254b86c-7456-47b3-8b7d-1590eef0f4a5",
                "project_description": "runtime docs",
            },
            {"document_rebind_boundary": FakeRebind()},
        ),
        (ProcessingStatusCommand, {"operation_id": "op-1"}, {"runtime_status_boundary": FakeStatus()}),
        (DocumentDeleteCommand, {"document_id": "550e8400-e29b-41d4-a716-446655440001", "version_token": "v1"}, {"canonical_document_service": FakeDelete()}),
        (Uuid4Command, {"count": 2}, {}),
        (EntityCreateCommand, {"entity_type": "files", "values": {"id": "550e8400-e29b-41d4-a716-446655440009", "path": "/tmp/doc.txt", "name": "doc.txt", "body_sha256": "0" * 64}}, {"entity_lifecycle_boundary": FakeLifecycle()}),
        (EntityListCommand, {"entity_type": "documents"}, {"entity_lifecycle_boundary": FakeLifecycle()}),
        (EntityGetCommand, {"entity_type": "documents", "entity_id": "550e8400-e29b-41d4-a716-446655440001"}, {"entity_lifecycle_boundary": FakeLifecycle()}),
        (EntityUpdateCommand, {"entity_type": "files", "entity_id": "550e8400-e29b-41d4-a716-446655440009", "values": {"owner_id": "550e8400-e29b-41d4-a716-446655440001"}}, {"entity_lifecycle_boundary": FakeLifecycle()}),
        (EntitySoftDeleteCommand, {"entity_type": "documents", "ids": ["550e8400-e29b-41d4-a716-446655440001"]}, {"entity_lifecycle_boundary": FakeLifecycle()}),
        (EntityUndeleteCommand, {"entity_type": "documents", "ids": ["550e8400-e29b-41d4-a716-446655440001"]}, {"entity_lifecycle_boundary": FakeLifecycle()}),
        (EntityHardDeleteCommand, {"entity_type": "documents", "ids": ["550e8400-e29b-41d4-a716-446655440001"]}, {"entity_lifecycle_boundary": FakeLifecycle()}),
        (EntityReferencesCommand, {"entity_type": "documents", "entity_id": "550e8400-e29b-41d4-a716-446655440001"}, {"entity_lifecycle_boundary": FakeLifecycle()}),
        (ChunkQuerySearchCommand, {"query": {"search_query": "hello"}}, {"search_orchestrator": lambda *_args, **_kwargs: {"status": "success", "data": {"results": []}}}),
        (DocStoreHealthCommand, {}, {}),
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


def test_runtime_configuration_installs_retrieval_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    from doc_store_server import main

    class FakeBoundary:
        pass

    boundary = FakeBoundary()
    monkeypatch.setattr(DocumentCreateCommand, "ingestion_boundary", None)
    monkeypatch.setattr(DocumentUpdateCommand, "ingestion_boundary", None)
    monkeypatch.setattr(DocumentExportCommand, "export_boundary", None)
    monkeypatch.setattr(DocumentDeleteCommand, "document_service", None)
    monkeypatch.setattr(DocumentGetCommand, "retrieval_boundary", None)
    monkeypatch.setattr(ChapterGetCommand, "retrieval_boundary", None)
    monkeypatch.setattr(ParagraphGetCommand, "retrieval_boundary", None)
    monkeypatch.setattr(ParagraphGetByNumberCommand, "retrieval_boundary", None)
    monkeypatch.setattr(ProcessingStatusCommand, "runtime_status_boundary", None)
    monkeypatch.setattr(ChunkQuerySearchCommand, "search_orchestrator", None)
    for command in (
        EntityCreateCommand,
        EntityListCommand,
        EntityGetCommand,
        EntityUpdateCommand,
        EntitySoftDeleteCommand,
        EntityUndeleteCommand,
        EntityHardDeleteCommand,
        EntityReferencesCommand,
    ):
        monkeypatch.setattr(command, "lifecycle_boundary", None)
    monkeypatch.setattr(DocStoreHealthCommand, "runtime_config", {})
    monkeypatch.setattr(main, "RuntimeIngestionBoundary", lambda *_args: object())
    monkeypatch.setattr(main, "installed_runtime_status", lambda: object())
    monkeypatch.setattr(main, "installed_search_orchestrator", lambda _config: object())
    monkeypatch.setattr(main, "installed_retrieval_boundary", lambda _config: boundary)
    monkeypatch.setattr(main, "installed_entity_lifecycle_service", lambda _config: boundary)
    monkeypatch.setattr(main, "installed_document_export_service", lambda _config: boundary)
    monkeypatch.setattr(main, "installed_document_service", lambda _config: boundary)

    main.configure_runtime_boundaries({"database": {"url": "postgresql://example/db"}})

    assert DocumentGetCommand.retrieval_boundary is boundary
    assert ChapterGetCommand.retrieval_boundary is boundary
    assert ParagraphGetCommand.retrieval_boundary is boundary
    assert ParagraphGetByNumberCommand.retrieval_boundary is boundary
    assert DocumentExportCommand.export_boundary is boundary
    assert DocumentDeleteCommand.document_service is boundary
    assert EntityCreateCommand.lifecycle_boundary is boundary
    assert EntityListCommand.lifecycle_boundary is boundary
    assert EntityUpdateCommand.lifecycle_boundary is boundary
    assert EntityHardDeleteCommand.lifecycle_boundary is boundary
