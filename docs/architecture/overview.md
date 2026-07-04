# Architecture overview

`doc-store` consists of three units:

1. Client library.
2. File watcher systemd service.
3. Server with PostgreSQL storage, filters, canonical text tree builder, vectorizer and Lark-based query language.

Canonical ingestion path:

```text
file/text -> extension filter -> normalized JSON -> Project/Chapter/Paragraph/Sentence tree -> PostgreSQL -> paragraph vectorization
```
