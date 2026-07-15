"""Database-backed health metrics for the doc-store runtime."""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import create_engine, text


def database_metrics(database_url: str | None, *, connect_timeout: int) -> dict[str, Any]:
    """Return document, paragraph, vectorization, and rate metrics."""
    if not database_url:
        return empty_database_metrics()
    engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": connect_timeout})
    try:
        with engine.connect() as connection:
            document_count = int(
                connection.execute(text("SELECT count(*) FROM documents WHERE deleted_at IS NULL")).scalar_one()
            )
            paragraph_count = int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM paragraphs AS p "
                        "JOIN documents AS d ON d.id = p.document_id "
                        "WHERE p.deleted_at IS NULL AND d.deleted_at IS NULL"
                    )
                ).scalar_one()
            )
            vectorization = [
                _vectorization_row(dict(row))
                for row in connection.execute(
                    text(
                        "SELECT d.id::text AS document_id, d.title, "
                        "count(DISTINCT sc.id) AS chunk_count, "
                        "count(DISTINCT sce.chunk_uuid) FILTER (WHERE sce.active IS TRUE) AS vectorized_chunk_count "
                        "FROM documents AS d "
                        "LEFT JOIN semantic_chunks AS sc ON sc.document_id = d.id AND sc.deleted_at IS NULL "
                        "LEFT JOIN semantic_chunk_embeddings AS sce ON sce.chunk_uuid = sc.id AND sce.active IS TRUE "
                        "WHERE d.deleted_at IS NULL "
                        "GROUP BY d.id, d.title "
                        "ORDER BY d.created_at ASC, d.id ASC"
                    )
                ).mappings()
            ]
            window_minutes = _rate_window_minutes()
            vectorized_recent = int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM documents AS d "
                        "WHERE d.deleted_at IS NULL "
                        "AND d.processing_status = 'completed' "
                        "AND d.processing_completed_at >= now() - (:window_minutes * interval '1 minute')"
                    ),
                    {"window_minutes": window_minutes},
                ).scalar_one()
            )
    finally:
        engine.dispose()
    return {
        "document_count": document_count,
        "paragraph_count": paragraph_count,
        "vectorization_by_document": vectorization,
        "vectorization_rate": {
            "window_minutes": window_minutes,
            "documents_vectorized": vectorized_recent,
            "documents_per_minute": vectorized_recent / window_minutes,
        },
    }


def empty_database_metrics() -> dict[str, Any]:
    """Return the metrics shape used when the database is unavailable."""
    return {
        "document_count": None,
        "paragraph_count": None,
        "vectorization_by_document": [],
        "vectorization_rate": {
            "window_minutes": _rate_window_minutes(),
            "documents_vectorized": None,
            "documents_per_minute": None,
        },
    }


def _vectorization_row(row: dict[str, Any]) -> dict[str, Any]:
    chunk_count = int(row.get("chunk_count") or 0)
    vectorized = int(row.get("vectorized_chunk_count") or 0)
    percent = 100.0 if chunk_count == 0 else round((vectorized / chunk_count) * 100, 2)
    return {
        "document_id": str(row.get("document_id")),
        "title": row.get("title"),
        "chunk_count": chunk_count,
        "vectorized_chunk_count": vectorized,
        "percent": percent,
    }


def _rate_window_minutes() -> int:
    return max(1, int(os.getenv("DOC_STORE_HEALTH_RATE_WINDOW_MINUTES", "5") or "5"))


__all__ = ["database_metrics", "empty_database_metrics"]
