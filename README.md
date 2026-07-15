# doc-store

`doc-store` is a documentation ingestion, canonical storage, and retrieval
system. The project publishes two products:

- `doc-store`, the server;
- `doc-store-client`, the independently installable PyPI client.

The server accepts document files and raw text, normalizes their content, and
publishes a canonical hierarchy only after a complete ingestion succeeds:

```text
Document
└── Chapter
    └── Paragraph
        └── SemanticChunk
```

Ingestion is atomic, versioned, and idempotent. A partially built document
version must never become visible. PostgreSQL is canonical storage, and
pgvector provides the semantic index.

Semantic processing is delegated to established component boundaries:

- `SvoChunkerClient` produces semantic chunks;
- `EmbeddingClient` produces vector representations;
- `chunk-metadata-adapter` creates canonical `SemanticChunk` metadata.

Retrieval supports full-text, semantic, and hybrid modes through the single
public `ChunkQuery` contract.

The server uses `mcp-proxy-adapter` as its exclusive runtime transport and
platform boundary, including JSON-RPC, OpenAPI, authorization, TLS or mTLS,
queues, WebSocket behavior, transfers, and proxy registration. The project does
not add a parallel transport surface.

`ServerManager` owns an explicit command manifest. The shared
`register_doc_store_commands(registry)` hook registers the same command set in
the main process and multiprocessing workers. `help` is derived from the live
registry, and `info` contains synchronized server documentation.
