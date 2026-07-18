"""Schema contract for semantic chunk text version lifecycle support."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_0017_migration_adds_lifecycle_lineage_and_version_dependents() -> None:
    migration = (ROOT / "migrations" / "versions" / "0017_semantic_chunk_version_lifecycle.py").read_text()

    for term in (
        "logical_chunk_id",
        "previous_version_id",
        "restored_from_version_id",
        "source_version_id",
        "status",
        "is_current",
        "valid_from",
        "valid_to",
        "comment",
        "actor",
        "operation_id",
        "deleted_at",
        "uq_semantic_chunk_versions_current_logical",
        "semantic_chunk_versions_status_valid",
        "chunk_version_id",
        "semantic_chunk_embeddings",
        "semantic_chunk_tokens",
        "semantic_chunk_metrics",
        "semantic_chunk_type_assignments",
    ):
        assert term in migration
