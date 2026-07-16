"""Authoritative, version-synchronized documentation for the info command."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, ClassVar, Final, Mapping, Protocol

from mcp_proxy_adapter.commands.base import CommandResult

from doc_store_server.commands.base import _DocStoreCommand


class CommandHelpRegistry(Protocol):
    """Adapter registry surface used to project the live command help catalog."""

    def get_all_commands_info(self) -> Mapping[str, Any]:
        """Return the adapter's current command/help payload."""


@dataclass(frozen=True, slots=True)
class InfoSection:
    """One stable, named section in the public server documentation."""

    name: str
    title: str
    content: str


@dataclass(frozen=True, slots=True)
class InfoDocument:
    """Typed complete documentation model with deterministic section selection."""

    identity: Mapping[str, str]
    sections: tuple[InfoSection, ...]
    command_reference: Mapping[str, Any]

    def section(self, name: str | None = None) -> InfoSection | None:
        """Select one section by name, or return no section for the full document."""

        if name is None:
            return None
        for section in self.sections:
            if section.name == name:
                return section
        raise KeyError(name)

    def as_data(self, selected: str | None = None) -> dict[str, Any]:
        """Serialize the document without changing its ordering or typed meaning."""

        chosen = self.section(selected)
        sections = self.sections if chosen is None else (chosen,)
        return {
            "identity": dict(self.identity),
            "command_reference": dict(self.command_reference),
            "sections": {
                item.name: {"title": item.title, "content": item.content}
                for item in sections
            },
            "selected_section": selected,
        }


SECTION_NAMES: Final[tuple[str, ...]] = (
    "identity",
    "architecture",
    "integrations",
    "chunking_runtime",
    "data_model",
    "semantic_chunk_metadata",
    "checksum_lifecycle",
    "query_and_export",
    "configuration",
    "secrets_tls_mtls",
    "postgresql_pgvector",
    "command_groups",
    "maintenance",
    "deployment_systemd",
    "environment",
    "upgrades_removal",
    "backup_recovery",
    "troubleshooting",
)


def _installed_version() -> str:
    """Read the build identity from the installed distribution metadata."""

    try:
        return version("doc-store")
    except PackageNotFoundError:
        return "unknown"


def _command_reference(registry: CommandHelpRegistry) -> dict[str, Any]:
    """Project the current adapter help payload without maintaining a catalog here."""

    payload = registry.get_all_commands_info()
    commands = payload.get("commands", payload)
    if not isinstance(commands, Mapping):
        raise TypeError("adapter command help payload must contain a mapping of commands")
    return {
        str(name): commands[name]
        for name in sorted(commands, key=lambda value: str(value))
    }


def build_info_document(registry: CommandHelpRegistry) -> InfoDocument:
    """Build complete documentation from installed identity and live adapter help."""

    build_version = _installed_version()
    live_commands = _command_reference(registry)
    command_names = ", ".join(live_commands) or "No commands are currently registered."
    sections = {
        "identity": (
            "doc-store is the documentation storage server. This document is generated "
            "by the installed build and is authoritative for the adapter command surface."
        ),
        "architecture": (
            "The server owns domain orchestration and delegates transport, JSON-RPC, "
            "OpenAPI, authorization, TLS/mTLS, queues, and WebSocket behavior to "
            "mcp-proxy-adapter. PostgreSQL is canonical storage; ingestion is versioned "
            "and atomic."
        ),
        "integrations": (
            "The supported boundaries are doc-store-client for client requests, "
            "mcp-proxy-adapter for transport and registration, SvoChunkerClient for "
            "chunking, EmbeddingClient for embeddings, and chunk-metadata-adapter for "
            "canonical ChunkQuery and SemanticChunk metadata."
        ),
        "chunking_runtime": (
            "doc-store does not implement a local text chunker. For paragraph, "
            "sentence, and semantic chunking strategies, ingestion passes the complete "
            "normalized text to the SVO runtime wrapper over SvoChunkerClient and "
            "persists only the returned SemanticChunk ranges and metadata. If the "
            "external chunker is unavailable, rejects a strategy, or violates the "
            "SemanticChunk contract, ingestion records a structured CHUNKER_* failure "
            "instead of falling back to local splitting."
        ),
        "data_model": (
            "The canonical hierarchy is Document -> Chapter -> Paragraph -> "
            "SemanticChunk. Document versions are immutable once visible, ingestion is "
            "idempotent, and partially built versions must not become queryable. "
            "Root tables are documents, chapters, paragraphs, and semantic_chunks. "
            "Dictionary tables normalize chunk_types, chunk_roles, chunk_statuses, "
            "block_types, languages, and categories. Chunk-owned child tables store "
            "classifier assignments, metrics, feedback, tokens, tags, links, and "
            "embeddings; these rows are subordinate to semantic_chunks and are not "
            "independent CRUD roots."
        ),
        "semantic_chunk_metadata": (
            "Semantic chunk metadata follows chunk-metadata-adapter. Write-time "
            "defaults fill identity, hashes, timestamps, enum classifiers, empty "
            "lists, usage flags, feedback counters, and per-classifier empty values "
            "such as DocBlock, system, new, paragraph, UNKNOWN, and uncategorized. "
            "Quality evaluation fields quality_score, coverage, cohesion, "
            "boundary_prev, and boundary_next remain unknown until an evaluator "
            "worker classifies and scores the chunk. The ingestion path writes "
            "tokens and bm25_tokens from text analysis and stores category tags in "
            "semantic_chunk_tags."
        ),
        "checksum_lifecycle": (
            "Checksum state belongs to files or documents. File checksums are stored "
            "as checksum_algorithm, content_sha256, and body_sha256 on files; documents "
            "store checksum_algorithm, content_sha256, body_sha256, and source_hash. "
            "Files expose needs_rechunk and needs_revectorize; documents expose "
            "needs_revectorize. Ingestion short-circuits unchanged supplied file "
            "checksums when no reprocessing flags are set, revectorizes active chunks "
            "when only revectorization is requested, and batch-marks old hierarchy rows "
            "deleted before writing changed chunks for a new file/version."
        ),
        "query_and_export": (
            "Retrieval uses the canonical relational hierarchy plus pgvector "
            "embeddings and full-text search vectors. Query payloads reconstruct "
            "semantic chunk classifier descriptions from dictionary assignment rows "
            "and export must perform the reverse mapping: database identifiers are "
            "projected back to adapter-visible enum/category/language values without "
            "duplicating canonical child-table data."
        ),
        "configuration": (
            "Keep application configuration transport-neutral and pass adapter "
            "configuration to the adapter-owned application boundary. Configure server "
            "address, port, logging, database connectivity, queue behavior, and feature "
            "settings through deployment configuration or environment variables."
        ),
        "secrets_tls_mtls": (
            "Keep credentials, signing material, and private keys outside source "
            "control. Configure TLS or mTLS through mcp-proxy-adapter; the doc-store "
            "application must not create a competing transport or certificate "
            "implementation."
        ),
        "postgresql_pgvector": (
            "PostgreSQL stores the canonical relational hierarchy and transaction state. "
            "pgvector stores semantic embeddings and supports semantic or hybrid "
            "retrieval; schema and migrations must preserve atomic document-version "
            "visibility."
        ),
        "command_groups": (
            "The live adapter registry is the command authority. Current command/help "
            "entries, including schemas and metadata, are projected below from the "
            "registry at execution time: "
            f"{command_names}"
        ),
        "maintenance": (
            "Routine maintenance starts with health, info, processing_status, command "
            "help, database health, migration state, queue/worker status, and service "
            "logs. Before rebuilding or deploying, bump the package and image version, "
            "build the package, install it into the deployed environment, run database "
            "migrations if the schema changed, restart the service, and verify live "
            "health and command availability through the adapter/proxy surface."
        ),
        "deployment_systemd": (
            "Run the adapter-owned server entrypoint with the installed package "
            "environment. A systemd unit should use an explicit virtual environment, "
            "configuration, restart policy, service account, and dependency ordering "
            "for PostgreSQL and the adapter-managed runtime."
        ),
        "environment": (
            "Use a dedicated virtual environment and pin compatible package versions "
            "for doc-store, mcp-proxy-adapter, svo-client, embed-client, and "
            "chunk-metadata-adapter. Record effective configuration through deployment "
            "diagnostics without exposing secrets."
        ),
        "upgrades_removal": (
            "Upgrade by installing the target package set, applying compatible database "
            "migrations, restarting workers and the service, and checking command/help "
            "identity. For removal, stop the service first, retain or explicitly archive "
            "database data, then remove the environment and unit only after recovery "
            "requirements are satisfied."
        ),
        "backup_recovery": (
            "Back up PostgreSQL with a consistent database dump and retain the matching "
            "configuration, migration state, and secret-management references. Recovery "
            "requires restoring the database, applying only compatible migrations, "
            "validating pgvector availability, and checking document-version visibility "
            "before reopening traffic."
        ),
        "troubleshooting": (
            "Start with adapter health, command help, effective configuration, queue "
            "status, database connectivity, migration state, and service logs. For "
            "missing commands compare the live registry/help payload with registration "
            "output; for retrieval defects inspect canonical chunk metadata, embeddings, "
            "and the active query mode."
        ),
    }
    ordered_sections = tuple(
        InfoSection(name=name, title=name.replace("_", " ").title(), content=sections[name])
        for name in SECTION_NAMES
    )
    return InfoDocument(
        identity={
            "package": "doc-store",
            "package_version": build_version,
            "build_version": build_version,
            "command": "info",
        },
        sections=ordered_sections,
        command_reference=live_commands,
    )


class InfoCommand(_DocStoreCommand):
    """Return complete synchronized server documentation or one named section."""

    name = "info"
    version = _installed_version()
    descr = "Return authoritative version-synchronized doc-store documentation."
    detailed_description = (
        "Returns the complete operational documentation model by default. "
        "Pass a named section to select exactly one deterministic section. "
        "Command reference data is projected from the live adapter registry/help payload."
    )
    schema_properties: ClassVar[dict[str, dict[str, str]]] = {
        "section": {
            "type": "string",
            "description": "Optional named section from the info documentation model.",
        }
    }
    required_fields: ClassVar[tuple[str, ...]] = ()
    parameter_docs = schema_properties
    return_contract = {
        "description": (
            "InfoDocument data with identity, named sections, and live command reference."
        )
    }
    usage_examples = [{}, {"section": "architecture"}]
    best_practices = [
        "Use the complete document for operational discovery and a named section "
        "for deterministic retrieval.",
        "Treat the command reference as live registry/help data, not as a static catalog.",
    ]
    stable_errors = {
        "UNKNOWN_SECTION": "The requested section is not present in SECTION_NAMES.",
        "INVALID_HELP_PAYLOAD": (
            "The adapter returned a command/help payload with an invalid shape."
        ),
    }

    async def execute(
        self,
        section: str | None = None,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        """Return the complete document or a selected named section."""

        del context
        from mcp_proxy_adapter.commands.command_registry import registry

        document = build_info_document(registry)
        if section is not None and section not in SECTION_NAMES:
            return CommandResult(
                success=False,
                error=f"UNKNOWN_SECTION: {section}",
                data={"known_sections": list(SECTION_NAMES)},
            )
        return CommandResult(success=True, data=document.as_data(section))


__all__ = [
    "InfoCommand",
    "InfoDocument",
    "InfoSection",
    "SECTION_NAMES",
    "build_info_document",
]
