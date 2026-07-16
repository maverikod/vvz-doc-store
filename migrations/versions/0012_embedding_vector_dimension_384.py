"""Move semantic chunk embeddings to the external 384-dimensional model.

Revision ID: 0012_embedding_vector_dimension_384
Revises: 0011_document_file_checksum_lifecycle
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op
from pgvector.sqlalchemy import VECTOR


revision = "0012_embedding_vector_dimension_384"
down_revision = "0011_document_file_checksum_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_semantic_chunk_embeddings_vector_cosine",
        table_name="semantic_chunk_embeddings",
    )
    op.execute("DELETE FROM semantic_chunk_embeddings")
    op.alter_column(
        "semantic_chunk_embeddings",
        "vector",
        type_=VECTOR(384),
        postgresql_using="vector::vector(384)",
    )
    op.execute("UPDATE documents SET needs_revectorize = TRUE WHERE deleted_at IS NULL")
    op.execute(
        "UPDATE files SET needs_revectorize = TRUE "
        "WHERE id IN (SELECT owner_id FROM documents WHERE deleted_at IS NULL)"
    )
    op.create_index(
        "ix_semantic_chunk_embeddings_vector_cosine",
        "semantic_chunk_embeddings",
        ["vector"],
        postgresql_using="hnsw",
        postgresql_ops={"vector": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index(
        "ix_semantic_chunk_embeddings_vector_cosine",
        table_name="semantic_chunk_embeddings",
    )
    op.execute("DELETE FROM semantic_chunk_embeddings")
    op.alter_column(
        "semantic_chunk_embeddings",
        "vector",
        type_=VECTOR(2),
        postgresql_using="vector::vector(2)",
    )
    op.create_index(
        "ix_semantic_chunk_embeddings_vector_cosine",
        "semantic_chunk_embeddings",
        ["vector"],
        postgresql_using="hnsw",
        postgresql_ops={"vector": "vector_cosine_ops"},
    )
