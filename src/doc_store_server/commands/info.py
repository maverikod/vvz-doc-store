"""Authoritative, version-synchronized documentation for the info command."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, ClassVar, Final, Mapping, Protocol

from mcp_proxy_adapter.commands.base import CommandResult

from doc_store_server.commands.registration import _DocStoreCommand


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
    "data_model",
    "configuration",
    "secrets_tls_mtls",
    "postgresql_pgvector",
    "command_groups",
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
        "data_model": (
            "The canonical hierarchy is Document -> Chapter -> Paragraph -> "
            "SemanticChunk. Document versions are immutable once visible, ingestion is "
            "idempotent, and partially built versions must not become queryable."
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
