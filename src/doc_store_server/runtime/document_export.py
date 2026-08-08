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


def _stable_uuid4(value: str) -> UUID:
    """Derive a repeatable UUID4-shaped identifier from a naming string.

    Identical to the helpers of the same name in ``ingestion.runtime_boundary``
    and ``ingestion.publication_mapper``; the three are worth consolidating, but
    importing a private name across package boundaries to save four lines would
    be the worse coupling.
    """

    raw = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


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
                primary_source_id = self._resolve_primary_source(
                    connection,
                    file_id=file_uuid,
                    document_id=document_uuid,
                    path=output_path,
                    byte_length=len(content),
                    content_sha256=content_sha256,
                )
                connection.execute(
                    text(
                        "INSERT INTO files "
                        "(id, owner_id, primary_source_id, path, name, media_type, "
                        "byte_length, char_count, "
                        "checksum_algorithm, content_sha256, body_sha256, is_deleted, "
                        "deleted_at, block_meta) "
                        "VALUES (:id, :owner_id, :primary_source_id, :path, :name, "
                        "'text/plain', :byte_length, "
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
                        "primary_source_id": primary_source_id,
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
            "primary_source_id": str(primary_source_id),
            "document_id": str(document_uuid),
            "file_id": str(file_uuid),
            "path": str(output_path),
            "byte_length": len(content),
            "char_count": len(body),
            "body_sha256": body_sha256,
        }


    @staticmethod
    def _resolve_primary_source(
        connection: Any,
        *,
        file_id: UUID,
        document_id: UUID,
        path: Path,
        byte_length: int,
        content_sha256: str,
    ) -> UUID:
        """Return the immutable original this exported File is bound to.

        Migration 0019 makes ``files.primary_source_id`` NOT NULL and unique, so
        an export cannot write its File row without first naming an original.
        For an export the exported bytes *are* the original: nothing else
        produced them, and the locator that describes them is the path they were
        written to.

        A File that already names an original keeps it, exactly as ingestion
        does. An original is immutable, and re-exporting over the same file id
        is not a reason to mint a second one; the ``files`` row's own checksums
        still move to the new content, which is what makes the two
        distinguishable afterwards.
        """

        existing = connection.execute(
            text("SELECT primary_source_id FROM files WHERE id = :file_id"),
            {"file_id": file_id},
        ).scalar_one_or_none()
        if existing is not None:
            return UUID(str(existing))

        source_id = _stable_uuid4(f"doc-store:primary-source:{file_id}:{content_sha256}")
        provenance = {
            "captured_by": "doc_store_server.runtime.document_export",
            "ingestion_command": "document_export",
            "exported_from_document_id": str(document_id),
            "file_id": str(file_id),
        }
        connection.execute(
            text(
                "INSERT INTO primary_sources "
                "(id, source_locator, media_type, recorded_encoding, byte_length, "
                "checksum_algorithm, original_sha256, provenance, created_at) "
                "VALUES (:id, :source_locator, 'text/plain', 'utf-8', :byte_length, "
                "'sha256', :original_sha256, CAST(:provenance AS jsonb), now()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": source_id,
                "source_locator": f"file://{path}",
                "byte_length": byte_length,
                "original_sha256": content_sha256,
                "provenance": json.dumps(provenance),
            },
        )
        return source_id


def installed_document_export_service(config: dict[str, Any] | None = None) -> DocumentExportService | None:
    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return DocumentExportService(database_url)


__all__ = ["DocumentExportService", "installed_document_export_service"]
