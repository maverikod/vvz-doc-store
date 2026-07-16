"""Runtime document export service."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from doc_store_server.db.health import database_url_from_config


class DocumentExportService:
    """Export a logical document into a text file and record the file row."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    def export_document(
        self,
        *,
        document_id: str,
        path: str,
        file_id: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        document_uuid = UUID(document_id)
        file_uuid = UUID(file_id) if file_id else uuid4()
        output_path = Path(path).expanduser()
        if output_path.exists() and not overwrite:
            raise FileExistsError(str(output_path))
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                document = connection.execute(
                    text(
                        "SELECT id, title, block_meta FROM documents "
                        "WHERE id = :document_id AND deleted_at IS NULL"
                    ),
                    {"document_id": document_uuid},
                ).mappings().one_or_none()
                if document is None:
                    raise LookupError(document_id)
                paragraphs = [
                    str(row)
                    for row in connection.execute(
                        text(
                            "SELECT text FROM paragraphs "
                            "WHERE document_id = :document_id AND deleted_at IS NULL "
                            "ORDER BY order_index ASC, id ASC"
                        ),
                        {"document_id": document_uuid},
                    ).scalars()
                ]
                body = "\n\n".join(paragraphs)
                if not body:
                    raise ValueError("document has no exportable text")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(body, encoding="utf-8")
                content = output_path.read_bytes()
                content_sha256 = hashlib.sha256(content).hexdigest()
                body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
                meta = {
                    "exported_from_document_id": str(document_uuid),
                    "exported_title": document.get("title"),
                    "checksum_algorithm": "sha256",
                    "content_sha256": content_sha256,
                    "body_sha256": body_sha256,
                }
                connection.execute(
                    text(
                        "INSERT INTO files "
                        "(id, owner_id, path, name, media_type, byte_length, char_count, "
                        "checksum_algorithm, content_sha256, body_sha256, is_deleted, "
                        "deleted_at, block_meta) "
                        "VALUES (:id, :owner_id, :path, :name, 'text/plain', :byte_length, "
                        ":char_count, 'sha256', :content_sha256, :body_sha256, FALSE, NULL, "
                        "CAST(:block_meta AS jsonb)) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "owner_id = EXCLUDED.owner_id, "
                        "path = EXCLUDED.path, "
                        "name = EXCLUDED.name, "
                        "media_type = EXCLUDED.media_type, "
                        "byte_length = EXCLUDED.byte_length, "
                        "char_count = EXCLUDED.char_count, "
                        "content_sha256 = EXCLUDED.content_sha256, "
                        "body_sha256 = EXCLUDED.body_sha256, "
                        "is_deleted = FALSE, "
                        "deleted_at = NULL, "
                        "updated_at = now(), "
                        "block_meta = EXCLUDED.block_meta"
                    ),
                    {
                        "id": file_uuid,
                        "owner_id": document_uuid,
                        "path": str(output_path),
                        "name": PurePosixPath(str(output_path)).name,
                        "byte_length": len(content),
                        "char_count": len(body),
                        "content_sha256": content_sha256,
                        "body_sha256": body_sha256,
                        "block_meta": json.dumps(meta),
                    },
                )
        finally:
            engine.dispose()
        return {
            "outcome": "exported",
            "document_id": str(document_uuid),
            "file_id": str(file_uuid),
            "path": str(output_path),
            "byte_length": len(content),
            "char_count": len(body),
            "body_sha256": body_sha256,
        }


def installed_document_export_service(config: dict[str, Any] | None = None) -> DocumentExportService | None:
    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return DocumentExportService(database_url)


__all__ = ["DocumentExportService", "installed_document_export_service"]
