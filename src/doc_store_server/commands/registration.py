"""Deterministic registration of the doc-store application command set."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Final, Literal, Protocol

from mcp_proxy_adapter.commands.base import Command
from mcp_proxy_adapter.commands.hooks import (
    register_auto_import_module,
    register_custom_commands_hook,
)

from doc_store_server.commands.chunk_query_search_command import ChunkQuerySearchCommand
from doc_store_server.commands.document_delete_command import DocumentDeleteCommand
from doc_store_server.commands.document_rebind_command import DocumentRebindCommand
from doc_store_server.commands.entity_lifecycle_commands import (
    EntityGetCommand,
    EntityHardDeleteCommand,
    EntityListCommand,
    EntityReferencesCommand,
    EntitySoftDeleteCommand,
    EntityUndeleteCommand,
)
from doc_store_server.commands.health_command import DocStoreHealthCommand
from doc_store_server.commands.ingestion_commands import (
    DocumentChunkCommand,
    DocumentCreateCommand,
    DocumentUpdateCommand,
)
from doc_store_server.commands.processing_status_command import ProcessingStatusCommand
from doc_store_server.commands.retrieval_commands import (
    ChapterGetCommand,
    DocumentGetCommand,
    ParagraphGetCommand,
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


DOC_STORE_COMMAND_MANIFEST: Final[tuple[CommandManifestEntry, ...]] = (
    CommandManifestEntry(
        "health",
        DocStoreHealthCommand,
        "doc_store_server.commands.health_command",
        "sync",
        "DocStoreHealthCommand.metadata",
        "DocStoreHealthCommand.get_schema",
    ),
    CommandManifestEntry(
        "document_get",
        DocumentGetCommand,
        "doc_store_server.commands.retrieval_commands",
        "sync",
        "DocumentGetCommand.metadata",
        "DocumentGetCommand.get_schema",
    ),
    CommandManifestEntry(
        "chapter_get",
        ChapterGetCommand,
        "doc_store_server.commands.retrieval_commands",
        "sync",
        "ChapterGetCommand.metadata",
        "ChapterGetCommand.get_schema",
    ),
    CommandManifestEntry(
        "paragraph_get",
        ParagraphGetCommand,
        "doc_store_server.commands.retrieval_commands",
        "sync",
        "ParagraphGetCommand.metadata",
        "ParagraphGetCommand.get_schema",
    ),
    CommandManifestEntry(
        "document_create",
        DocumentCreateCommand,
        "doc_store_server.commands.ingestion_commands",
        "queue",
        "DocumentCreateCommand.metadata",
        "DocumentCreateCommand.get_schema",
    ),
    CommandManifestEntry(
        "document_update",
        DocumentUpdateCommand,
        "doc_store_server.commands.ingestion_commands",
        "queue",
        "DocumentUpdateCommand.metadata",
        "DocumentUpdateCommand.get_schema",
    ),
    CommandManifestEntry(
        "document_chunk",
        DocumentChunkCommand,
        "doc_store_server.commands.ingestion_commands",
        "queue",
        "DocumentChunkCommand.metadata",
        "DocumentChunkCommand.get_schema",
    ),
    CommandManifestEntry(
        "document_rebind",
        DocumentRebindCommand,
        "doc_store_server.commands.document_rebind_command",
        "sync",
        "DocumentRebindCommand.metadata",
        "DocumentRebindCommand.get_schema",
    ),
    CommandManifestEntry(
        "processing_status",
        ProcessingStatusCommand,
        "doc_store_server.commands.processing_status_command",
        "sync",
        "ProcessingStatusCommand.metadata",
        "ProcessingStatusCommand.get_schema",
    ),
    CommandManifestEntry(
        "document_delete",
        DocumentDeleteCommand,
        "doc_store_server.commands.document_delete_command",
        "sync",
        "DocumentDeleteCommand.metadata",
        "DocumentDeleteCommand.get_schema",
    ),
    CommandManifestEntry(
        "entity_list",
        EntityListCommand,
        "doc_store_server.commands.entity_lifecycle_commands",
        "sync",
        "EntityListCommand.metadata",
        "EntityListCommand.get_schema",
    ),
    CommandManifestEntry(
        "entity_get",
        EntityGetCommand,
        "doc_store_server.commands.entity_lifecycle_commands",
        "sync",
        "EntityGetCommand.metadata",
        "EntityGetCommand.get_schema",
    ),
    CommandManifestEntry(
        "entity_soft_delete",
        EntitySoftDeleteCommand,
        "doc_store_server.commands.entity_lifecycle_commands",
        "sync",
        "EntitySoftDeleteCommand.metadata",
        "EntitySoftDeleteCommand.get_schema",
    ),
    CommandManifestEntry(
        "entity_undelete",
        EntityUndeleteCommand,
        "doc_store_server.commands.entity_lifecycle_commands",
        "sync",
        "EntityUndeleteCommand.metadata",
        "EntityUndeleteCommand.get_schema",
    ),
    CommandManifestEntry(
        "entity_hard_delete",
        EntityHardDeleteCommand,
        "doc_store_server.commands.entity_lifecycle_commands",
        "sync",
        "EntityHardDeleteCommand.metadata",
        "EntityHardDeleteCommand.get_schema",
    ),
    CommandManifestEntry(
        "entity_references",
        EntityReferencesCommand,
        "doc_store_server.commands.entity_lifecycle_commands",
        "sync",
        "EntityReferencesCommand.metadata",
        "EntityReferencesCommand.get_schema",
    ),
    CommandManifestEntry(
        "chunk_query_search",
        ChunkQuerySearchCommand,
        "doc_store_server.commands.chunk_query_search_command",
        "sync",
        "ChunkQuerySearchCommand.metadata",
        "ChunkQuerySearchCommand.get_schema",
    ),
)

DOC_STORE_COMMAND_MODULE_MANIFEST: Final[tuple[str, ...]] = (
    "doc_store_server.commands.health_command",
    "doc_store_server.commands.retrieval_commands",
    "doc_store_server.commands.ingestion_commands",
    "doc_store_server.commands.document_rebind_command",
    "doc_store_server.commands.processing_status_command",
    "doc_store_server.commands.document_delete_command",
    "doc_store_server.commands.entity_lifecycle_commands",
    "doc_store_server.commands.chunk_query_search_command",
)
DOC_STORE_QUEUED_COMMAND_MODULES: Final[tuple[str, ...]] = (
    "doc_store_server.commands.ingestion_commands",
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
    "ChunkQuerySearchCommand",
    "CommandManifestEntry",
    "DOC_STORE_COMMAND_MANIFEST",
    "DOC_STORE_COMMAND_MODULE_MANIFEST",
    "DOC_STORE_QUEUED_COMMAND_MODULES",
    "DocStoreHealthCommand",
    "DocumentChunkCommand",
    "DocumentCreateCommand",
    "DocumentDeleteCommand",
    "DocumentGetCommand",
    "DocumentRebindCommand",
    "DocumentUpdateCommand",
    "EntityGetCommand",
    "EntityHardDeleteCommand",
    "EntityListCommand",
    "EntityReferencesCommand",
    "EntitySoftDeleteCommand",
    "EntityUndeleteCommand",
    "ChapterGetCommand",
    "ParagraphGetCommand",
    "ProcessingStatusCommand",
    "register_doc_store_commands",
]
