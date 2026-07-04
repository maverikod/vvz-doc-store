# Canonical document model

The server converts all accepted inputs into a unified text tree.

## Entities

### Project

A large logical document block: book, documentation set, collection, manual, or imported source package.

Required attributes:

- id
- title
- source_name
- metadata

### Chapter

A section inside a project.

Required attributes:

- id
- project_id
- title
- order_index

### Paragraph

A structural text unit inside a chapter.

Required attributes:

- id
- chapter_id
- order_index
- level: int
- title
- body

### Sentence

A sentence inside a paragraph.

Required attributes:

- id
- paragraph_id
- order_index
- text
- language

## Vectorization

Vectorization is paragraph-based. If paragraph body is too large for the embedding model or retrieval policy, the vectorizer splits it into semantic blocks using a sliding window and stores vectors for those blocks while preserving the paragraph linkage.
