# Codex project context: doc-store

## Authority split

Use Code Analysis Server project `ff997eab-d809-4cb9-b805-9dff4df60c6d`
through MCP Proxy first for project search, detailed preview, AST, usage,
dependency, import, complexity, duplication, lint, type, and quality analysis.
Use the checkout at `/home/vasilyvz/projects/tools/doc-store` for project
content mutation, Git, tests, builds, and project execution. Editing is local
`apply_patch` only; proxy mutation, CAS mutation, and AI Editor are prohibited.

Use the registered `doc-store` plan in Plan Manager through MCP Proxy as the
only normative plan truth. Files in the configured local plan-export directory,
when present, must be byte-identical copies produced by `plan_export`; they are
explicitly non-normative and must not be reconstructed or edited. Do not use
them to answer current plan-state questions when Plan Manager is available.

## Product boundary

The plan defines two published products:

- `doc-store`, the documentation server;
- `doc-store-client`, the independently installable PyPI client.

The server uses `mcp-proxy-adapter` exclusively for transport, JSON-RPC,
OpenAPI, authorization, TLS or mTLS, queues, WebSocket behavior, and proxy
registration. Do not introduce a separate FastAPI or REST transport surface.

## Domain and storage model

The canonical hierarchy is:

`Document -> Chapter -> Paragraph -> SemanticChunk`

PostgreSQL is canonical storage and pgvector is the semantic index. Document
ingestion is atomic, versioned, and idempotent; a partially built document
version must never become visible.

Chunking uses `SvoChunkerClient`. Embeddings use `EmbeddingClient`. Canonical
`SemanticChunk` metadata is produced through `chunk-metadata-adapter`.

## Search and command surface

Support full-text, semantic, and hybrid search. `ChunkQuery` is the single
public search contract.

`ServerManager` owns an explicit command manifest. A single
`register_doc_store_commands(registry)` hook must register the same command set
in the main process and multiprocessing workers. `help` is generated from the
live registry, while `info` contains complete synchronized server
documentation.

## Planning and implementation discipline

HRS is human-owned. MRS is structured and implementation-free. GS, TS, and AS
must reproduce their parents semantically. One AS changes exactly one
project-relative code file and includes explicit verification. Mechanical plan
validation precedes semantic scoring. Project edits remain local even when the
work is driven by a Plan Manager AS.

The intended fresh decomposition starts with removal of conflicting legacy
implementation, then a minimal adapter-based skeleton, then early
`ServerManager` and shared registration infrastructure, followed by ingestion,
storage, and search behavior. Treat this ordering as project context, not as a
substitute for live Plan Manager state.
