"""Shared SQL and mapping helpers for adapter SemanticChunk payloads."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


CLASSIFIER_SELECT_SQL = """
               COALESCE(ct.descr, sc.chunk_type, 'DocBlock') AS chunk_type_descr,
               cr.descr AS role_descr,
               cs.descr AS status_descr,
               bt.descr AS block_type_descr,
               lang.descr AS language_descr,
               cat.descr AS category_descr
"""

CLASSIFIER_JOIN_SQL = """
        LEFT JOIN semantic_chunk_type_assignments AS scta ON scta.chunk_uuid = sc.id
        LEFT JOIN chunk_types AS ct ON ct.id = COALESCE(scta.chunk_type_id, sc.chunk_type_id)
        LEFT JOIN semantic_chunk_role_assignments AS scra ON scra.chunk_uuid = sc.id
        LEFT JOIN chunk_roles AS cr ON cr.id = COALESCE(scra.role_id, sc.role_id)
        LEFT JOIN semantic_chunk_status_assignments AS scsa ON scsa.chunk_uuid = sc.id
        LEFT JOIN chunk_statuses AS cs ON cs.id = COALESCE(scsa.status_id, sc.status_id)
        LEFT JOIN semantic_chunk_block_type_assignments AS scbta ON scbta.chunk_uuid = sc.id
        LEFT JOIN block_types AS bt ON bt.id = COALESCE(scbta.block_type_id, sc.block_type_id)
        LEFT JOIN semantic_chunk_language_assignments AS scla ON scla.chunk_uuid = sc.id
        LEFT JOIN languages AS lang ON lang.id = COALESCE(scla.language_id, sc.language_id)
        LEFT JOIN semantic_chunk_category_assignments AS scca ON scca.chunk_uuid = sc.id
        LEFT JOIN categories AS cat ON cat.id = COALESCE(scca.category_id, sc.category_id)
"""


def chunk_payload_from_row(
    row: Mapping[str, Any],
    row_value: Callable[[Mapping[str, Any], str], Any],
) -> dict[str, Any]:
    block_meta = row_value(row, "block_meta")
    if not isinstance(block_meta, Mapping):
        raise TypeError("database row block_meta must be a mapping")
    metadata = dict(block_meta)
    chunk_type = row.get("chunk_type_descr") or row.get("chunk_type") or "DocBlock"
    classifiers = {
        "type": chunk_type,
        "role": row.get("role_descr"),
        "status": row.get("status_descr"),
        "block_type": row.get("block_type_descr"),
        "language": row.get("language_descr"),
        "category": row.get("category_descr"),
    }
    for key, value in classifiers.items():
        if value is not None:
            metadata[key] = value
    return {
        "uuid": str(row_value(row, "id")),
        "source_id": str(row_value(row, "document_id")),
        "block_id": str(row_value(row, "paragraph_id")),
        "type": chunk_type,
        "role": classifiers["role"],
        "status": classifiers["status"],
        "block_type": classifiers["block_type"],
        "language": classifiers["language"],
        "category": classifiers["category"],
        "body": row_value(row, "text"),
        "text": row_value(row, "text"),
        "ordinal": row_value(row, "order_index"),
        "start": row_value(row, "source_start"),
        "end": row_value(row, "source_end"),
        "block_meta": metadata,
    }


__all__ = ["CLASSIFIER_JOIN_SQL", "CLASSIFIER_SELECT_SQL", "chunk_payload_from_row"]
