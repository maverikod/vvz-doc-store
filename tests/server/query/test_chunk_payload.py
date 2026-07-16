"""Tests for SemanticChunk export payload reconstruction from normalized dictionaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from doc_store_server.query.chunk_payload import CLASSIFIER_JOIN_SQL, chunk_payload_from_row


def _row_value(row: Mapping[str, Any], name: str) -> Any:
    return row[name]


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "11111111-1111-4111-8111-111111111111",
        "document_id": "22222222-2222-4222-8222-222222222222",
        "paragraph_id": "33333333-3333-4333-8333-333333333333",
        "order_index": 7,
        "text": "chunk body",
        "source_start": 10,
        "source_end": 20,
        "chunk_type": "stale-root-type",
        "block_meta": {
            "type": "stale-meta-type",
            "role": "stale-meta-role",
            "status": "stale-meta-status",
            "block_type": "stale-meta-block-type",
            "language": "stale-meta-language",
            "category": "stale-meta-category",
            "project": "doc-store",
        },
        "chunk_type_descr": "DocBlock",
        "role_descr": "system",
        "status_descr": "indexed",
        "block_type_descr": "paragraph",
        "language_descr": "UNKNOWN",
        "category_descr": "uncategorized",
    }
    row.update(overrides)
    return row


def test_chunk_payload_exports_dictionary_descriptions_not_internal_ids_or_stale_metadata() -> None:
    payload = chunk_payload_from_row(_row(), _row_value)

    assert payload["type"] == "DocBlock"
    assert payload["role"] == "system"
    assert payload["status"] == "indexed"
    assert payload["block_type"] == "paragraph"
    assert payload["language"] == "UNKNOWN"
    assert payload["category"] == "uncategorized"
    assert payload["block_meta"]["type"] == "DocBlock"
    assert payload["block_meta"]["role"] == "system"
    assert payload["block_meta"]["status"] == "indexed"
    assert payload["block_meta"]["block_type"] == "paragraph"
    assert payload["block_meta"]["language"] == "UNKNOWN"
    assert payload["block_meta"]["category"] == "uncategorized"


def test_chunk_payload_falls_back_to_root_chunk_type_for_pre_assignment_rows() -> None:
    row = _row(chunk_type_descr=None, chunk_type="DocBlock")

    payload = chunk_payload_from_row(row, _row_value)

    assert payload["type"] == "DocBlock"
    assert payload["block_meta"]["type"] == "DocBlock"


def test_classifier_joins_read_child_assignments_before_compatibility_columns() -> None:
    assert "semantic_chunk_status_assignments AS scsa" in CLASSIFIER_JOIN_SQL
    assert "chunk_statuses AS cs ON cs.id = COALESCE(scsa.status_id, sc.status_id)" in CLASSIFIER_JOIN_SQL
    assert "categories AS cat ON cat.id = COALESCE(scca.category_id, sc.category_id)" in CLASSIFIER_JOIN_SQL
