"""Deterministic registration of the doc-store application command set."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Final, Literal, Protocol

from mcp_proxy_adapter.commands.base import Command
from mcp_proxy_adapter.commands.hooks import (
    register_auto_import_module,
    register_custom_commands_hook,
)


ExecutionMode = Literal["sync", "queue"]
CommandClass = type[Command]


class CommandRegistry(Protocol):
    """Minimal adapter registry contract consumed by doc-store registration."""

    def register(self, command_class: CommandClass, command_type: str) -> None:
        """Register one command class with the adapter."""


@dataclass(frozen=True, slots=True)
class CommandManifestEntry:
    """Immutable identity record for one public application command."""

    command_name: str
    command_class: CommandClass
    import_module: str
    execution_mode: ExecutionMode
    metadata_identity: str
    schema_identity: str


class _DocStoreCommand(Command):
    """Metadata-only command contract; behavior is implemented by later AS."""

    version: ClassVar[str] = "0.1.0"
    descr: ClassVar[str] = ""
    category: ClassVar[str] = "doc-store"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False
    detailed_description: ClassVar[str] = (
        "Public doc-store command contract. Business behavior is implemented "
        "by later atomic command-handler steps."
    )
    schema_properties: ClassVar[dict[str, dict[str, str]]] = {}
    required_fields: ClassVar[tuple[str, ...]] = ()
    parameter_docs: ClassVar[dict[str, dict[str, str]]] = {}
    return_contract: ClassVar[dict[str, str]] = {
        "description": "Command-specific result contract."
    }
    usage_examples: ClassVar[list[dict[str, Any]]] = []
    best_practices: ClassVar[list[str]] = [
        "Use the adapter registry and generated help as the command authority."
    ]
    stable_errors: ClassVar[dict[str, str]] = {
        "NOT_IMPLEMENTED": "Command handler behavior is owned by a later atomic step."
    }

    async def execute(self, **kwargs: Any) -> Any:
        """Prevent accidental business execution before handler AS implement it."""

        raise NotImplementedError(f"{self.name} handler is not implemented in registration")

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        """Return the complete public metadata contract for help generation."""

        return {
            "name": cls.name,
            "version": cls.version,
            "description": cls.descr,
            "category": cls.category,
            "author": cls.author,
            "email": cls.email,
            "detailed_description": cls.detailed_description,
            "parameters": cls.parameter_docs,
            "return_value": cls.return_contract,
            "usage_examples": cls.usage_examples,
            "error_cases": cls.stable_errors,
            "best_practices": cls.best_practices,
        }

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """Return the public JSON schema contract for this command."""

        schema: dict[str, Any] = {
            "type": "object",
            "properties": dict(cls.schema_properties),
            "required": list(cls.required_fields),
            "additionalProperties": False,
        }
        if cls.use_queue:
            schema["x-use-queue"] = True
        return schema


class CreateDocumentCommand(_DocStoreCommand):
    name = "create_document"
    descr = "Create or replace one document version from adapter-owned input."
    use_queue = True
    detailed_description = (
        "Accepts a document creation request and delegates future ingestion behavior "
        "to the ingestion orchestration layer without owning transport behavior."
    )
    schema_properties = {
        "document_id": {"type": "string", "description": "Optional document UUID."},
        "title": {"type": "string", "description": "Document title."},
        "source": {"type": "object", "description": "Adapter-provided source descriptor."},
    }
    required_fields = ("title", "source")
    parameter_docs = schema_properties
    return_contract = {"description": "Queued document creation result."}
    usage_examples = [{"title": "Example", "source": {"kind": "text"}}]
    best_practices = ["Use adapter transfer primitives for large file payloads."]


class UpdateDocumentCommand(CreateDocumentCommand):
    name = "update_document"
    descr = "Create a new version for an existing document."
    required_fields = ("document_id", "source")
    return_contract = {"description": "Queued document update result."}


class ProcessingStatusCommand(_DocStoreCommand):
    name = "processing_status"
    descr = "Return processing state for one document operation."
    schema_properties = {
        "operation_id": {"type": "string", "description": "Operation identifier."}
    }
    required_fields = ("operation_id",)
    parameter_docs = schema_properties
    return_contract = {"description": "Current processing state and diagnostics."}
    usage_examples = [{"operation_id": "00000000-0000-4000-8000-000000000000"}]
    best_practices = ["Poll only operation identifiers returned by document commands."]


class GetDocumentCommand(_DocStoreCommand):
    name = "get_document"
    descr = "Return one document by UUID."
    schema_properties = {
        "document_id": {"type": "string", "description": "Document UUID."}
    }
    required_fields = ("document_id",)
    parameter_docs = schema_properties
    return_contract = {"description": "Document payload."}
    usage_examples = [{"document_id": "00000000-0000-4000-8000-000000000000"}]
    best_practices = ["Use UUID identifiers returned by create_document."]


class GetChapterCommand(GetDocumentCommand):
    name = "get_chapter"
    descr = "Return one chapter by UUID."
    schema_properties = {
        "chapter_id": {"type": "string", "description": "Chapter UUID."}
    }
    required_fields = ("chapter_id",)


class GetParagraphCommand(GetDocumentCommand):
    name = "get_paragraph"
    descr = "Return one paragraph by UUID."
    schema_properties = {
        "paragraph_id": {"type": "string", "description": "Paragraph UUID."}
    }
    required_fields = ("paragraph_id",)


class DeleteDocumentCommand(_DocStoreCommand):
    name = "delete_document"
    descr = "Delete or tombstone one document."
    use_queue = True
    schema_properties = {
        "document_id": {"type": "string", "description": "Document UUID."}
    }
    required_fields = ("document_id",)
    parameter_docs = schema_properties
    return_contract = {"description": "Queued deletion result."}
    usage_examples = [{"document_id": "00000000-0000-4000-8000-000000000000"}]
    best_practices = ["Delete by document UUID, not source filename."]


class ChunkQueryCommand(_DocStoreCommand):
    name = "chunk_query"
    descr = "Search chunks through the canonical ChunkQuery contract."
    use_queue = True
    schema_properties = {
        "query": {"type": "object", "description": "ChunkQuery payload."}
    }
    required_fields = ("query",)
    parameter_docs = schema_properties
    return_contract = {"description": "ChunkQueryResponse-compatible search result."}
    usage_examples = [{"query": {"text": "adapter boundary"}}]
    best_practices = ["Use chunk-metadata-adapter ChunkQuery fields only."]


DOC_STORE_COMMAND_MANIFEST: Final[tuple[CommandManifestEntry, ...]] = (
    CommandManifestEntry(
        "create_document",
        CreateDocumentCommand,
        "doc_store_server.commands.registration",
        "queue",
        "CreateDocumentCommand.metadata",
        "CreateDocumentCommand.get_schema",
    ),
    CommandManifestEntry(
        "update_document",
        UpdateDocumentCommand,
        "doc_store_server.commands.registration",
        "queue",
        "UpdateDocumentCommand.metadata",
        "UpdateDocumentCommand.get_schema",
    ),
    CommandManifestEntry(
        "processing_status",
        ProcessingStatusCommand,
        "doc_store_server.commands.registration",
        "sync",
        "ProcessingStatusCommand.metadata",
        "ProcessingStatusCommand.get_schema",
    ),
    CommandManifestEntry(
        "get_document",
        GetDocumentCommand,
        "doc_store_server.commands.registration",
        "sync",
        "GetDocumentCommand.metadata",
        "GetDocumentCommand.get_schema",
    ),
    CommandManifestEntry(
        "get_chapter",
        GetChapterCommand,
        "doc_store_server.commands.registration",
        "sync",
        "GetChapterCommand.metadata",
        "GetChapterCommand.get_schema",
    ),
    CommandManifestEntry(
        "get_paragraph",
        GetParagraphCommand,
        "doc_store_server.commands.registration",
        "sync",
        "GetParagraphCommand.metadata",
        "GetParagraphCommand.get_schema",
    ),
    CommandManifestEntry(
        "delete_document",
        DeleteDocumentCommand,
        "doc_store_server.commands.registration",
        "queue",
        "DeleteDocumentCommand.metadata",
        "DeleteDocumentCommand.get_schema",
    ),
    CommandManifestEntry(
        "chunk_query",
        ChunkQueryCommand,
        "doc_store_server.commands.registration",
        "queue",
        "ChunkQueryCommand.metadata",
        "ChunkQueryCommand.get_schema",
    ),
)

DOC_STORE_COMMAND_MODULE_MANIFEST: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(entry.import_module for entry in DOC_STORE_COMMAND_MANIFEST)
)
DOC_STORE_QUEUED_COMMAND_MODULES: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(
        entry.import_module
        for entry in DOC_STORE_COMMAND_MANIFEST
        if entry.execution_mode == "queue"
    )
)


def register_doc_store_commands(registry: CommandRegistry) -> None:
    """Register every declared doc-store application command exactly once."""

    for entry in DOC_STORE_COMMAND_MANIFEST:
        registry.register(entry.command_class, "custom")


setattr(
    register_doc_store_commands,
    "__auto_import_modules__",
    DOC_STORE_COMMAND_MODULE_MANIFEST,
)
for module_name in DOC_STORE_QUEUED_COMMAND_MODULES:
    register_auto_import_module(module_name)

register_custom_commands_hook(register_doc_store_commands)


__all__ = [
    "ChunkQueryCommand",
    "CommandManifestEntry",
    "CreateDocumentCommand",
    "DOC_STORE_COMMAND_MANIFEST",
    "DOC_STORE_COMMAND_MODULE_MANIFEST",
    "DOC_STORE_QUEUED_COMMAND_MODULES",
    "DeleteDocumentCommand",
    "GetChapterCommand",
    "GetDocumentCommand",
    "GetParagraphCommand",
    "ProcessingStatusCommand",
    "UpdateDocumentCommand",
    "register_doc_store_commands",
]
