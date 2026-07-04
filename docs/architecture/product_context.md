# Product context

`doc-store` is a documentation storage system.

It consists of three main parts:

1. Client library.
2. File watcher.
3. Server.

The file watcher is a systemd service. It reads configured directories, selects files with known extensions, and sends them to the server through the client library.

The client is a library for working with the server API.

The server accepts files or raw texts from the client, applies extension-specific filters, converts content into unified JSON objects, transforms text into a canonical hierarchy, saves the hierarchy to PostgreSQL, and asynchronously vectorizes paragraphs.

Canonical hierarchy:

```text
Project / Book / large block
└── Chapter
    └── Paragraph(level: int)
        ├── title
        ├── body
        └── Sentence(language: str)
```

The server also provides a SQL-like query language based on Lark.
