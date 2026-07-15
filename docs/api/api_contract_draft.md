# API contract draft

Initial API surface:

```text
POST /api/v1/documents/text
POST /api/v1/documents/file
GET  /api/v1/documents/{document_id}
GET  /api/v1/vectorization/jobs/{job_id}
```

## POST /api/v1/documents/text

Accepts raw text with source metadata.

Input:

- text
- source_name
- metadata

Output:

- ingestion_id
- status

## POST /api/v1/documents/file

Accepts file upload. The server determines processing pipeline from extension and filter registry.

Output:

- ingestion_id
- filename
- status
