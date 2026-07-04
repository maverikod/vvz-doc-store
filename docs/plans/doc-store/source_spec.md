# Source Specification — doc-store

<!-- non-binding -->
Canonical level-1 HRS/source_spec. Binding paragraphs carry stable labels and feed `spec.yaml`.
<!-- /non-binding -->

## Product purpose

{a1b2} The system shall provide a documentation storage service named `doc-store` that accepts files and raw texts, normalizes them, stores a canonical document tree, vectorizes searchable text units, and exposes structured retrieval through an SQL-like query language.

{c3d4} The system shall be composed of three principal runtime parts: a reusable API client library, a file watcher service, and a server.

{e5f6} PostgreSQL shall be the canonical persistent database; local files after ingestion shall not be the source of truth for canonical document data.

## Client library

{g7h8} The client library shall hide HTTP/API details and provide operations for uploading raw text, uploading files, checking ingestion status, and submitting query requests.

{i9j0} The file watcher and future external tools shall use the client library instead of duplicating server API request logic.

## File watcher

{k1l2} The file watcher shall run as a systemd-oriented service and shall read watched directories, recursion mode, known extensions, server URL, and authentication settings from configuration.

{m3n4} The file watcher shall collect files from configured directories, filter them by known extensions, and send accepted files to the server through the client library.

{o5p6} Unsupported files shall be skipped with diagnostics and shall not be sent as unknown opaque payloads.

## Server ingestion

{q7r8} The server shall accept both raw text and file uploads from clients.

{s9t0} The server shall route files through extension-specific filters selected by a known-extension registry.

{u1v2} Each filter shall transform source input into a unified JSON object containing source metadata and extracted textual content suitable for canonical tree construction.

{w3x4} Filter or ingestion failures shall be reported with diagnostics and shall not corrupt already stored canonical data.

## Canonical document tree

{y5z6} The server shall transform normalized content into a canonical hierarchy: Project, Chapter, Paragraph, and Sentence.

{b7c8} A Project represents a large logical block such as a book, documentation set, manual, or imported source collection and owns ordered Chapters.

{d9e0} A Chapter belongs to exactly one Project, preserves order inside the Project, and owns ordered Paragraphs.

{f1g2} A Paragraph belongs to exactly one Chapter, preserves order inside the Chapter, and has `level: int`, `title`, and `body`.

{h3i4} A Sentence belongs to exactly one Paragraph, preserves order inside the Paragraph, and has `language`.

## Storage and vectorization

{j5k6} PostgreSQL shall store source records, ingestion jobs, Projects, Chapters, Paragraphs, Sentences, vectorization jobs, vector records, and query-relevant metadata.

{l7m8} The system shall preserve traceability from every canonical node back to its source record and ingestion job.

{n9o0} Schema evolution shall be managed through migrations.

{p1q2} Vectorization shall run asynchronously from ingestion so ingestion can complete without waiting for vector creation.

{r3s4} The vectorizer shall create vectors for Paragraphs and store vector metadata including provider/model identifier, dimensions, status, and source Paragraph reference.

{t5u6} If a Paragraph is too large for the embedding model or retrieval policy, the vectorizer shall split it into ordered semantic blocks using a configurable sliding-window strategy with overlap.

{v7w8} Provider-specific model names shall remain outside product requirements unless an approved provider integration task explicitly requires them; agent prompts may map generic model roles to Claude/Anthropic or Codex/OpenAI execution models.

## Query language

{x9y0} The server shall expose a SQL-like query language parsed by Lark.

{z1a2} The initial query language shall support SELECT, FROM, WHERE, comparison operators, AND/OR, and LIMIT.

{b3d4} Query execution shall operate over canonical entities and PostgreSQL-backed metadata, returning rows and diagnostics instead of internal errors.

## Planning, agents, and quality

{e5g6} Planning shall follow the strict cascade: source_spec, machine-readable spec, global steps, tactical steps, and atomic steps.

{h7j8} Lower-level planning artifacts shall be derived from upper-level artifacts and shall preserve traceability through stable labels and concept identifiers.

{k9m0} Claude and Codex entry prompts shall instruct agents to read the standards, canonical plan structure, HRS/source_spec, machine spec, global/tactical plan files, and file structure before implementation.

{n1p2} The project shall keep a documented file structure separating client, file watcher, server API, server core, database, filters, ingestion, query language, vectorization, migrations, systemd units, tests, standards, and plans.

{q3s4} New implementation shall use typed Python, explicit modules, tests for implemented requirements, and conservative architecture with clear contracts.
