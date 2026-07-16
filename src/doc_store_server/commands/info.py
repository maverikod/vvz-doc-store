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
    "vectorization_runtime",
    "data_model",
    "semantic_chunk_metadata",
    "checksum_lifecycle",
    "ownership_model",
    "query_and_export",
    "semantic_relations",
    "corpus_audit",
    "unit_title_editing",
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
            "instead of falling back to local splitting. Chunking does not call the "
            "embedding service; it marks the file/document for later vectorization."
        ),
        "vectorization_runtime": (
            "Embeddings are produced only by the external embedding service through "
            "embed-client. The embeddings_rebuild command is the public vectorizer "
            "entry point and is queued for large corpora. It reads documents/chunks "
            "in document batches selected by needs_revectorize flags or explicit "
            "document_id/all_documents parameters, calls embed-client in text batches, "
            "writes semantic_chunk_embeddings in database batches, clears processed "
            "needs_revectorize flags, and never performs chunking. It writes separate "
            "vectorizer_activity.jsonl, vectorizer_processed.jsonl, and "
            "vectorizer_errors.jsonl logs. Activity events are written before "
            "embedding a document and after successful document persistence so health "
            "can show the current file from logs and suppress it once the database has "
            "active vectors for every chunk in that document. Processed chunk events "
            "include chunk_id, document_id, and the shared chunk_preview. "
            "If the embedding service is unavailable, the vectorizer returns "
            "embedding_unavailable instead of crashing and suppresses repeated "
            "unavailable log entries until a later successful batch records recovery."
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
            "checksums when no reprocessing flags are set, leaves revectorization "
            "work to the separate vectorizer when only revectorization is requested, "
            "and batch-marks old hierarchy rows deleted before writing changed chunks "
            "for a new file/version."
        ),
        "ownership_model": (
            "Every addressable entity table exposes a nullable owner_id field: "
            "projects, files, documents, chapters, paragraphs, semantic_chunks, "
            "chunk_types, chunk_roles, chunk_statuses, block_types, languages, and "
            "categories. owner_id references the shared entity_uuid_registry, so an "
            "owner may be any registered entity, not only a project. The canonical "
            "hierarchy defaults are file.owner_id -> project when a file is assigned "
            "to a project, document.owner_id -> file, chapter.owner_id -> document, "
            "paragraph.owner_id -> chapter, and semantic_chunk.owner_id -> paragraph. "
            "Use entity_rebind_owner for ownership changes: "
            "{\"entity_type\":\"files\",\"ids\":[\"<file_uuid4>\"],"
            "\"owner_id\":\"<project_uuid4>\"}. Use owner_id:null only for explicit "
            "unbinding. entity_update remains available for single root CRUD rows, "
            "but entity_rebind_owner is the public command for batch ownership "
            "changes across all entity scopes."
        ),
        "query_and_export": (
            "Retrieval uses the canonical relational hierarchy plus pgvector "
            "embeddings and full-text search vectors. Query payloads reconstruct "
            "semantic chunk classifier descriptions from dictionary assignment rows "
            "and export must perform the reverse mapping: database identifiers are "
            "projected back to adapter-visible enum/category/language values without "
            "duplicating canonical child-table data. For semantic text search through "
            "chunk_query_search, clients send search_query with hybrid_search=true, "
            "bm25_weight=0, and semantic_weight=1; the server obtains the query vector "
            "through embed-client and executes the semantic branch against active "
            "semantic_chunk_embeddings. For deterministic source scans, clients may "
            "filter by block_meta.source_name or document identifiers and page with "
            "limit/max_results plus zero-based offset; structured scans are ordered "
            "by document creation, chunk order_index, and chunk id."
        ),
        "semantic_relations": (
            "semantic_relations is the public corpus-wide embedding comparison API. "
            "It compares stored active semantic_chunk_embeddings at document, file, "
            "paragraph, or chunk level. The command accepts relation=similar or "
            "relation=opposite, metric=cosine_distance or cosine_similarity, bounded "
            "candidate/pair limits, scope filters, and returns groups containing item "
            "ids, previews, source names, parsed 7d numbers, scores, model, provider, "
            "model_version, dimension, pagination, and diagnostics. Similar mode "
            "selects distance below or similarity above the threshold; opposite mode "
            "selects distance above or similarity below the threshold. Example similar "
            "call: {\"level\":\"chunk\",\"relation\":\"similar\",\"metric\":\"cosine_distance\","
            "\"threshold\":0.2,\"max_candidates\":40,\"max_pairs\":200,\"limit\":3}. "
            "Example opposite call: {\"level\":\"paragraph\",\"relation\":\"opposite\","
            "\"metric\":\"cosine_similarity\",\"threshold\":0.2,\"project\":\"7d\"}. "
            "Operational nuances: keep max_candidates and max_pairs bounded on large "
            "corpora; compare model/provider/model_version/dimension before treating "
            "scores from different runs as equivalent; use seven_d_number/source_name "
            "filters for focused analysis; treat groups as candidates until a domain "
            "review or evaluator worker classifies the relation."
        ),
        "corpus_audit": (
            "corpus_audit is the public indexed-corpus inspection API. Modes are "
            "inventory, corrections, conflicts, exact_duplicates, topics, and "
            "unit_title_capabilities. inventory parses 7d-NN identifiers from source "
            "names or leading text and reports missing, duplicate, non-monotonic, and "
            "metadata-mismatched numbering. corrections and conflicts return marker "
            "evidence with locations and previews. exact_duplicates normalizes text "
            "and groups equal chunk bodies. topics returns an ordered source/topic map. "
            "unit_title_capabilities documents which hierarchy title fields can be "
            "edited through current public APIs. Example inventory call: "
            "{\"mode\":\"inventory\",\"project\":\"7d\",\"limit\":50}. Example corrections "
            "call: {\"mode\":\"corrections\",\"markers\":[\"корректировка\",\"уточнение\"],"
            "\"limit\":20}. Example duplicate call: {\"mode\":\"exact_duplicates\","
            "\"min_length\":120,\"include_aggregators\":false,\"limit\":20}. "
            "Nuances: corrections/conflicts are evidence finders, not final truth; "
            "conflict grouping currently combines marker evidence and source/topic "
            "context, while semantic contradiction classification belongs to the later "
            "evaluator worker; inventory reports parsed and metadata 7d numbers so "
            "import mistakes can be separated from source-text numbering."
        ),
        "unit_title_editing": (
            "Current public editing support is deliberately narrow. documents.title "
            "is editable via entity_update because documents are root CRUD entities. "
            "chapters.heading exists in the database but chapters are not root update "
            "targets in entity_update. Paragraphs and semantic chunks have no direct "
            "title column; title-like values may exist as block_meta.title, but public "
            "write support is not exposed because changing unit text/metadata must "
            "preserve search vectors, embeddings, checksums, and ingestion provenance. "
            "To change a document title, call entity_update with "
            "{\"entity_type\":\"documents\",\"entity_id\":\"<uuid4>\","
            "\"updates\":{\"title\":\"New title\"}}. To inspect current support, call "
            "corpus_audit with {\"mode\":\"unit_title_capabilities\"}. Do not edit "
            "paragraph or chunk titles by direct database mutation: that would bypass "
            "checksum, full-text, embedding, and export consistency rules."
        ),
        "configuration": (
            "Configure doc-store with the installed config JSON, systemd/default "
            "environment, or process environment. Database settings may be supplied as "
            "DOC_STORE_DATABASE_URL or DATABASE_URL, or through the configured database "
            "section consumed by database_url_from_config. Registration metadata, proxy "
            "URL, server URL, command transport, queue settings, logging, and feature "
            "flags are passed to mcp-proxy-adapter; doc-store must not create a parallel "
            "transport stack."
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
            "and keep client and server versions equal. Build the package, install it "
            "into the deployed environment, run database migrations if the schema "
            "changed, restart the service, and verify live health, command help, info, "
            "runtime ingestion, search, semantic_relations, and corpus_audit through the "
            "adapter/proxy surface. The deployed post-deploy pipeline is "
            "scripts/verify_runtime_capabilities.py; it must be run against the real "
            "server after deploy, not only against local tests. It creates temporary "
            "documents, verifies file transfer, ingestion, rechunk, rebind, vectorization, "
            "full-text search, semantic search, retrieval, entity lifecycle, full command "
            "help schemas, metadata paradigm, info sections, corpus_audit, and "
            "semantic_relations. Treat a pipeline failure as a release blocker until "
            "the failing command is fixed, rebuilt with a new version, redeployed, and "
            "retested."
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
