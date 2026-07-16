"""Installed runtime retrieval boundary for document hierarchy commands."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text

from doc_store_server.commands.retrieval_commands import InvalidVersionError
from doc_store_server.db.health import database_url_from_config


class RuntimeRetrievalBoundary:
    """Read typed document hierarchy units from the installed PostgreSQL schema."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    async def get_document(self, document_id: UUID, source_version: int | None = None) -> dict[str, Any]:
        row = self._one(
            """
            SELECT id::text, source_version, source_path, source_name, source_hash,
                   title, processing_status, block_meta, created_at, updated_at,
                   (SELECT array_agg(c.id::text ORDER BY c.order_index)
                    FROM chapters AS c
                    WHERE c.document_id = d.id AND c.deleted_at IS NULL) AS chapter_ids,
                   (SELECT array_agg(p.id::text ORDER BY p.order_index)
                    FROM paragraphs AS p
                    WHERE p.document_id = d.id AND p.deleted_at IS NULL) AS paragraph_ids,
                   (SELECT array_agg(sc.id::text ORDER BY sc.order_index)
                    FROM semantic_chunks AS sc
                    WHERE sc.document_id = d.id AND sc.deleted_at IS NULL) AS chunk_ids
            FROM documents AS d
            WHERE d.id = :identifier AND d.deleted_at IS NULL
            """,
            document_id,
            source_version,
        )
        return _json_row(row)

    async def get_chapter(self, chapter_id: UUID, source_version: int | None = None) -> dict[str, Any]:
        row = self._one(
            """
            SELECT c.id::text, c.document_id::text, d.source_version, c.order_index,
                   c.heading, c.level, c.source_start, c.source_end, c.block_meta,
                   (SELECT array_agg(p.id::text ORDER BY p.order_index)
                    FROM paragraphs AS p
                    WHERE p.chapter_id = c.id AND p.deleted_at IS NULL) AS paragraph_ids,
                   (SELECT array_agg(sc.id::text ORDER BY sc.order_index)
                    FROM semantic_chunks AS sc
                    WHERE sc.chapter_id = c.id AND sc.deleted_at IS NULL) AS chunk_ids
            FROM chapters AS c
            JOIN documents AS d ON d.id = c.document_id
            WHERE c.id = :identifier AND c.deleted_at IS NULL AND d.deleted_at IS NULL
            """,
            chapter_id,
            source_version,
        )
        return _json_row(row)

    async def get_paragraph(self, paragraph_id: UUID, source_version: int | None = None) -> dict[str, Any]:
        row = self._one(
            """
            SELECT p.id::text, p.document_id::text, p.chapter_id::text, d.source_version,
                   p.order_index, p.text, p.language, p.source_start, p.source_end,
                   p.quality_score, p.search_weight, p.block_meta,
                   (SELECT array_agg(sc.id::text ORDER BY sc.order_index)
                    FROM semantic_chunks AS sc
                    WHERE sc.paragraph_id = p.id AND sc.deleted_at IS NULL) AS chunk_ids
            FROM paragraphs AS p
            JOIN documents AS d ON d.id = p.document_id
            WHERE p.id = :identifier AND p.deleted_at IS NULL AND d.deleted_at IS NULL
            """,
            paragraph_id,
            source_version,
        )
        return _json_row(row)

    async def get_paragraph_by_number(
        self,
        document_id: UUID,
        paragraph_number: int,
        source_version: int | None = None,
    ) -> dict[str, Any]:
        row = self._one(
            """
            SELECT p.id::text, p.document_id::text, p.chapter_id::text, d.source_version,
                   (p.order_index + 1) AS paragraph_number, p.order_index, p.text,
                   p.language, p.source_start, p.source_end, p.quality_score,
                   p.search_weight, p.block_meta,
                   (SELECT array_agg(sc.id::text ORDER BY sc.order_index)
                    FROM semantic_chunks AS sc
                    WHERE sc.paragraph_id = p.id AND sc.deleted_at IS NULL) AS chunk_ids
            FROM paragraphs AS p
            JOIN documents AS d ON d.id = p.document_id
            WHERE d.id = :identifier
              AND p.order_index = :paragraph_index
              AND p.deleted_at IS NULL
              AND d.deleted_at IS NULL
            """,
            document_id,
            source_version,
            extra_params={"paragraph_index": paragraph_number - 1},
        )
        return _json_row(row)

    def _one(
        self,
        sql: str,
        identifier: UUID,
        source_version: int | None,
        *,
        extra_params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        sql
                        + (
                            " AND d.source_version = :source_version"
                            if source_version is not None
                            else ""
                        )
                    ),
                    {
                        "identifier": identifier,
                        "source_version": source_version,
                        **dict(extra_params or {}),
                    },
                ).mappings().one_or_none()
        finally:
            engine.dispose()
        if row is None:
            if source_version is not None:
                raise InvalidVersionError(f"source_version {source_version} is not visible")
            raise LookupError(str(identifier))
        return row


def installed_retrieval_boundary(config: Mapping[str, Any] | None = None) -> RuntimeRetrievalBoundary | None:
    """Create the installed retrieval boundary from env/config, when possible."""

    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return RuntimeRetrievalBoundary(database_url)


def _json_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif isinstance(value, tuple):
            result[key] = list(value)
        else:
            result[key] = value
    return result


__all__ = ["RuntimeRetrievalBoundary", "installed_retrieval_boundary"]
