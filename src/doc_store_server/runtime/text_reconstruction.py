"""Runtime text reconstruction from current ordered chunk payloads."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text

from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.previews import chunk_preview


DEFAULT_MAX_CHARS = 200_000
DEFAULT_LIMIT = 10_000
METADATA_FILTER_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class _ChunkTextRow:
    chunk_id: str
    paragraph_id: str
    chapter_id: str
    document_id: str
    order_index: int
    paragraph_order_index: int
    text: str
    source_start: int | None
    source_end: int | None
    source_name: str | None
    source_path: str | None
    file_id: str | None


class TextReconstructionService:
    """Assemble chapter/source text from normalized current semantic chunk payloads."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    def assemble_chapter_text(
        self,
        *,
        chapter_id: str | None = None,
        document_id: str | None = None,
        file_id: str | None = None,
        source_name: str | None = None,
        source_path: str | None = None,
        project_id: str | None = None,
        metadata_filters: Mapping[str, Any] | None = None,
        include_context: bool = False,
        max_chars: int = DEFAULT_MAX_CHARS,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not chapter_id and not any((document_id, file_id, source_name, source_path, project_id, metadata_filters)):
            raise ValueError("chapter_id or at least one selector is required")
        rows = self._select_rows(
            chapter_id=chapter_id,
            document_id=document_id,
            file_id=file_id,
            source_name=source_name,
            source_path=source_path,
            project_id=project_id,
            metadata_filters=metadata_filters,
            limit=limit,
            offset=offset,
        )
        if not rows:
            raise LookupError("no current chunk text found for chapter selector")
        return self._assemble(
            rows,
            entity="chapter",
            selector={
                "chapter_id": chapter_id,
                "document_id": document_id,
                "file_id": file_id,
                "source_name": source_name,
                "source_path": source_path,
                "project_id": project_id,
                "metadata_filters": dict(metadata_filters or {}),
            },
            include_context=include_context,
            max_chars=max_chars,
            limit=limit,
            offset=offset,
        )

    def reconstruct_source_file(
        self,
        *,
        file_id: str | None = None,
        document_id: str | None = None,
        source_name: str | None = None,
        source_path: str | None = None,
        project_id: str | None = None,
        metadata_filters: Mapping[str, Any] | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not any((file_id, document_id, source_name, source_path, project_id, metadata_filters)):
            raise ValueError("at least one source selector is required")
        rows = self._select_rows(
            document_id=document_id,
            file_id=file_id,
            source_name=source_name,
            source_path=source_path,
            project_id=project_id,
            metadata_filters=metadata_filters,
            limit=limit,
            offset=offset,
        )
        if not rows:
            raise LookupError("no current chunk text found for source selector")
        return self._assemble(
            rows,
            entity="source_file",
            selector={
                "file_id": file_id,
                "document_id": document_id,
                "source_name": source_name,
                "source_path": source_path,
                "project_id": project_id,
                "metadata_filters": dict(metadata_filters or {}),
            },
            include_context=False,
            max_chars=max_chars,
            limit=limit,
            offset=offset,
        )

    def _select_rows(
        self,
        *,
        chapter_id: str | None = None,
        document_id: str | None = None,
        file_id: str | None = None,
        source_name: str | None = None,
        source_path: str | None = None,
        project_id: str | None = None,
        metadata_filters: Mapping[str, Any] | None = None,
        limit: int,
        offset: int,
    ) -> tuple[_ChunkTextRow, ...]:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        params: dict[str, Any] = {"limit": _positive_int(limit, "limit"), "offset": _non_negative_int(offset, "offset")}
        where = [
            "d.deleted_at IS NULL",
            "c.deleted_at IS NULL",
            "p.deleted_at IS NULL",
            "sc.deleted_at IS NULL",
            "NOT d.is_deleted",
            "NOT c.is_deleted",
            "NOT p.is_deleted",
            "NOT sc.is_deleted",
        ]
        _add_uuid_filter(where, params, "chapter_id", chapter_id, "sc.chapter_id")
        _add_uuid_filter(where, params, "document_id", document_id, "sc.document_id")
        if file_id is not None:
            params["file_id"] = str(UUID(file_id))
            where.append(
                "(d.owner_id = :file_id OR d.source_upload_id = :file_id "
                "OR sc.block_meta ->> 'file_id' = :file_id)"
            )
        if project_id is not None:
            params["project_id"] = str(UUID(project_id))
            where.append(
                "(d.owner_id = :project_id OR sc.block_meta ->> 'project_id' = :project_id "
                "OR sc.block_meta ->> 'project' = :project_id)"
            )
        if source_name is not None:
            params["source_name"] = source_name
            where.append("(d.source_name = :source_name OR sc.block_meta ->> 'source_name' = :source_name)")
        if source_path is not None:
            params["source_path"] = source_path
            where.append("(d.source_path = :source_path OR sc.block_meta ->> 'source_path' = :source_path)")
        for index, (key, value) in enumerate(sorted((metadata_filters or {}).items())):
            if not isinstance(key, str) or not METADATA_FILTER_KEY_RE.fullmatch(key):
                raise ValueError(
                    "metadata filter keys must match ^[A-Za-z_][A-Za-z0-9_]{0,127}$"
                )
            param = f"meta_{index}"
            params[param] = str(value)
            where.append(f"sc.block_meta ->> '{key}' = :{param}")
        sql = (
            "SELECT sc.id::text AS chunk_id, sc.paragraph_id::text AS paragraph_id, "
            "sc.chapter_id::text AS chapter_id, sc.document_id::text AS document_id, "
            "sc.order_index, p.order_index AS paragraph_order_index, sct.text, "
            "sc.source_start, sc.source_end, d.source_name, d.source_path, "
            "COALESCE(sc.block_meta ->> 'file_id', d.owner_id::text, d.source_upload_id::text) AS file_id "
            "FROM semantic_chunks AS sc "
            "JOIN semantic_chunk_texts AS sct ON sct.chunk_uuid = sc.id "
            "JOIN paragraphs AS p ON p.id = sc.paragraph_id "
            "JOIN chapters AS c ON c.id = sc.chapter_id "
            "JOIN documents AS d ON d.id = sc.document_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY d.created_at ASC, d.id ASC, c.order_index ASC, p.order_index ASC, sc.order_index ASC, sc.id ASC "
            "LIMIT :limit OFFSET :offset"
        )
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                return tuple(
                    _ChunkTextRow(
                        chunk_id=row["chunk_id"],
                        paragraph_id=row["paragraph_id"],
                        chapter_id=row["chapter_id"],
                        document_id=row["document_id"],
                        order_index=int(row["order_index"]),
                        paragraph_order_index=int(row["paragraph_order_index"]),
                        text=str(row["text"]),
                        source_start=row["source_start"],
                        source_end=row["source_end"],
                        source_name=row["source_name"],
                        source_path=row["source_path"],
                        file_id=row["file_id"],
                    )
                    for row in connection.execute(text(sql), params).mappings()
                )
        finally:
            engine.dispose()

    def _assemble(
        self,
        rows: Iterable[_ChunkTextRow],
        *,
        entity: str,
        selector: Mapping[str, Any],
        include_context: bool,
        max_chars: int,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        max_chars = _non_negative_int(max_chars, "max_chars")
        row_tuple = tuple(rows)
        pieces: list[str] = []
        range_map: list[dict[str, Any]] = []
        cursor = 0
        truncated = False
        previous_paragraph: str | None = None
        for row in row_tuple:
            if previous_paragraph is None:
                separator = ""
            elif row.paragraph_id == previous_paragraph:
                separator = " "
            else:
                separator = "\n\n"
            candidate = f"{separator}{row.text}"
            available = max_chars - cursor if max_chars else len(candidate)
            if max_chars and available <= 0:
                truncated = True
                break
            emitted = candidate[:available] if max_chars else candidate
            if len(emitted) < len(candidate):
                truncated = True
            pieces.append(emitted)
            text_start = cursor + len(separator) if emitted.startswith(separator) else cursor
            text_end = cursor + len(emitted)
            range_map.append(
                {
                    "chunk_id": row.chunk_id,
                    "paragraph_id": row.paragraph_id,
                    "chapter_id": row.chapter_id,
                    "document_id": row.document_id,
                    "file_id": row.file_id,
                    "order_index": row.order_index,
                    "paragraph_order_index": row.paragraph_order_index,
                    "text_start": text_start,
                    "text_end": text_end,
                    "source_start": row.source_start,
                    "source_end": row.source_end,
                    "preview": chunk_preview(row.text),
                }
            )
            cursor += len(emitted)
            previous_paragraph = row.paragraph_id
            if truncated:
                break
        body = "".join(pieces)
        source_names = sorted({row.source_name for row in row_tuple if row.source_name})
        source_paths = sorted({row.source_path for row in row_tuple if row.source_path})
        document_ids = sorted({row.document_id for row in row_tuple})
        chapter_ids = sorted({row.chapter_id for row in row_tuple})
        return {
            "entity": entity,
            "selector": {key: value for key, value in selector.items() if value not in (None, {}, [])},
            "text": body,
            "preview": chunk_preview(body),
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "char_count": len(body),
            "chunk_count": len(range_map),
            "paragraph_count": len({item["paragraph_id"] for item in range_map}),
            "document_ids": document_ids,
            "chapter_ids": chapter_ids,
            "source_names": source_names,
            "source_paths": source_paths,
            "range_map": range_map,
            "truncated": truncated,
            "limit": limit,
            "offset": offset,
            "max_chars": max_chars,
            "versioning": {
                "mode": "current_active",
                "note": "Historical chunk versions are not returned unless future version selectors are added.",
            },
            "context": {"included": bool(include_context)},
        }


def _add_uuid_filter(where: list[str], params: dict[str, Any], key: str, value: str | None, column: str) -> None:
    if value is None:
        return
    params[key] = str(UUID(value))
    where.append(f"{column} = :{key}")


def _positive_int(value: int, field: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{field} must be >= 1")
    return parsed


def _non_negative_int(value: int, field: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{field} must be >= 0")
    return parsed


def installed_text_reconstruction_service(config: dict[str, Any] | None = None) -> TextReconstructionService | None:
    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return TextReconstructionService(database_url)


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_CHARS",
    "TextReconstructionService",
    "installed_text_reconstruction_service",
]
