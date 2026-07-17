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
            "sentence, and semantic chunking strategies, ingestion uses the SVO "
            "runtime wrapper over SvoChunkerClient and persists only returned "
            "SemanticChunk ranges and metadata. The ingestion pipeline first chunks "
            "the normalized document text by paragraph, then sends the paragraph "
            "texts as a sentence batch and maps local sentence offsets back to "
            "document coordinates. If the "
            "external chunker is unavailable, rejects a strategy, or violates the "
            "SemanticChunk contract, ingestion records a structured CHUNKER_* failure "
            "instead of falling back to local splitting. Chunking does not call the "
            "embedding service; it marks the file/document for later vectorization."
        ),
        "vectorization_runtime": (
            "Embeddings are produced only by the external embedding service through "
            "embed-client. The embeddings_rebuild command is the public vectorizer "
            "entry point and is queued for large corpora. It reads documents/entities "
            "in document batches selected by needs_revectorize flags or explicit "
            "document_id/all_documents parameters. It sends paragraph and "
            "semantic_chunk/sentence text to embed-client in text batches. If "
            "DOC_STORE_EMBEDDING_DIRECT_TEXT_MAX_CHARS is positive, small document "
            "or file bodies at or below that character limit may be embedded directly; "
            "larger document vectors are calculated as the arithmetic mean of active "
            "paragraph vectors, falling back to chunk vectors only when no paragraph "
            "vectors are available; larger file vectors are calculated as the "
            "arithmetic mean of document vectors. This prevents very large document "
            "or file bodies from being sent to embed-client. It writes "
            "semantic_chunk_embeddings in database batches "
            "using entity_type/entity_id for all vectorized levels and chunk_uuid only "
            "for semantic_chunk compatibility with existing semantic search, clears "
            "processed needs_revectorize flags, and never performs chunking. When an "
            "embed-client result item includes bm25_tokens, the vectorizer replaces that chunk's "
            "semantic_chunk_tokens rows for token_kind='bm25_tokens' in the same "
            "persistence transaction; when bm25_tokens is absent, existing tokens are "
            "left unchanged. It writes separate "
            "vectorizer_activity.jsonl, vectorizer_processed.jsonl, and "
            "vectorizer_errors.jsonl logs. Activity events are written before "
            "embedding a document and after successful document persistence so health "
            "can show the current file from logs and suppress it once the database has "
            "active vectors for the document plus every paragraph and semantic chunk "
            "in that document. Processed entity events include entity_type, entity_id, "
            "document_id, and the shared chunk_preview for textual lower-level units. "
            "If the embedding service is unavailable, the vectorizer returns "
            "embedding_unavailable instead of crashing and suppresses repeated "
            "unavailable log entries until a later successful batch records recovery."
        ),
        "data_model": (
            "The canonical hierarchy is Document -> Chapter -> Paragraph -> "
            "SemanticChunk. Document versions are immutable once visible, ingestion is "
            "idempotent, and partially built versions must not become queryable. "
            "Root tables are documents, chapters, paragraphs, and semantic_chunks. "
            "Ingestion of raw text or transferred files now persists both editable "
            "paragraph units and sentence-level semantic chunks: the external SVO "
            "client is called for paragraph boundaries and sentence boundaries, "
            "paragraphs store paragraph text, and semantic_chunks store individual "
            "sentences in semantic_chunk_texts. The vectorizer stores active vectors "
            "for file, document, paragraph, and semantic_chunk/sentence levels while "
            "preserving a selectable retrieval block size. "
            "The full body of every semantic chunk is stored outside the structural "
            "chunk row in semantic_chunk_texts, keyed by chunk_uuid with text, "
            "text_sha256, char_count, timestamps, and optional payload metadata. "
            "semantic_chunks keeps identifiers, ownership, ordering, classifier ids, "
            "metrics hooks, lifecycle state, and block_meta; its legacy text column is "
            "a transition field and is not the authoritative body source. Search, "
            "vectorization, semantic relation discovery, metadata previews, and CRUD "
            "text responses join semantic_chunk_texts and alias that payload as text. "
            "Dictionary tables normalize chunk_types, chunk_roles, chunk_statuses, "
            "block_types, languages, and categories. Chunk-owned child tables store "
            "classifier assignments, metrics, feedback, tokens, tags, and links; "
            "embeddings are stored in semantic_chunk_embeddings for every vectorized "
            "entity level through entity_type/entity_id, with chunk_uuid retained for "
            "semantic chunk compatibility."
        ),
        "semantic_chunk_metadata": (
            "Semantic chunk metadata follows chunk-metadata-adapter. Write-time "
            "defaults fill identity, hashes, timestamps, enum classifiers, empty "
            "lists, usage flags, feedback counters, and per-classifier empty values "
            "such as DocBlock, system, needs_review, paragraph, UNKNOWN, and "
            "uncategorized for newly ingested or rechunked chunks. "
            "Sentence granularity is represented by semantic_chunk_texts, "
            "block_type=sentence, and block_meta.unit_type=sentence, matching "
            "chunk-metadata-adapter >= 3.4.1. "
            "Quality evaluation fields quality_score, coverage, cohesion, "
            "boundary_prev, and boundary_next remain unknown until an evaluator "
            "worker classifies and scores the chunk. The ingestion path writes "
            "tokens and bm25_tokens from text analysis and stores category tags in "
            "semantic_chunk_tags. When existing chunk text changes through the "
            "persistence boundary, stale machine-derived metadata is invalidated: "
            "quality metrics, feedback, tokens, tags, embeddings, summary, title, "
            "classification provenance, and old category are cleared; status becomes "
            "needs_review and category becomes uncategorized until classifier and "
            "vectorizer workers process the new text. entity_update is the public "
            "text writer for paragraph text and semantic chunk sentence text; "
            "semantic_chunk_metadata_update is the public writer for safe chunk "
            "classifier metadata. It updates dictionary compatibility "
            "columns, normalized classifier assignment child rows, block_meta, and "
            "semantic_chunk_tags in one transaction. Allowed fields are type, role, "
            "status, block_type, language, category, tags, summary, title, and "
            "classification provenance. Forbidden fields include text/body, embeddings, "
            "tokens, bm25_tokens, quality_score, coverage, cohesion, boundary_prev, "
            "and boundary_next. For weak local or open-source classifiers, include "
            "classification.provider, model, model_version, prompt_version, confidence, "
            "evidence, and review_status='machine' so a later evaluator worker can "
            "audit or replace the machine hypothesis. Use dry_run before large updates "
            "and batch per-chunk patches through items."
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
            "changes across all entity scopes. Use entity_owner_tree with an entity_id "
            "to inspect the owner_id subordinate tree; each node returns entity_type, "
            "id, preview, and children, with max_depth and max_children_per_node guards "
            "for large corpora."
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
            "semantic_chunk_embeddings. Hierarchy-aware semantic search is controlled "
            "by the separate semantic_refinement command parameter, not by adding "
            "fields to ChunkQuery. Each omitted semantic_refinement field is resolved "
            "from search.semantic_refinement in the installed config; if absent there, "
            "the server uses built-in constants. With semantic_refinement.enabled=true, "
            "the server first searches across file, document, paragraph, and semantic chunk "
            "vectors without choosing a level, then refines document/file candidates "
            "down the linked hierarchy using the separate threshold, candidate_limit, "
            "and result_limit window. Strong refined child results may evict weaker "
            "tail results from a full window. For deterministic source scans, clients may "
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
            "targets in entity_update. Paragraph text and semantic chunk sentence "
            "text are editable through entity_update with entity_type paragraphs or "
            "semantic_chunks. Sentence text writes go through semantic_chunk_texts and "
            "invalidate stale metrics, tokens, tags, embeddings, summary/title, "
            "classification, status, and category so workers can rebuild them. "
            "Paragraphs and semantic chunks have no direct title column; title-like "
            "values may exist as block_meta.title. "
            "To change a document title, call entity_update with "
            "{\"entity_type\":\"documents\",\"entity_id\":\"<uuid4>\","
            "\"values\":{\"title\":\"New title\"}}. To change paragraph text, call "
            "{\"entity_type\":\"paragraphs\",\"entity_id\":\"<paragraph_uuid4>\","
            "\"values\":{\"text\":\"New paragraph text\"}}. To change sentence text, call "
            "{\"entity_type\":\"semantic_chunks\",\"entity_id\":\"<chunk_uuid4>\","
            "\"values\":{\"text\":\"New sentence text\"}}. To inspect current support, call "
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
