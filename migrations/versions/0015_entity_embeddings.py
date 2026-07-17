"""Generalize embeddings from semantic chunks to hierarchy entities."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015_entity_embeddings"
down_revision = "0014_semantic_chunk_texts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "semantic_chunk_embeddings",
        sa.Column(
            "entity_type",
            sa.String(length=64),
            nullable=False,
            server_default="semantic_chunk",
        ),
    )
    op.add_column(
        "semantic_chunk_embeddings",
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        "UPDATE semantic_chunk_embeddings SET entity_id = chunk_uuid WHERE entity_id IS NULL"
    )
    op.alter_column("semantic_chunk_embeddings", "entity_id", nullable=False)
    op.alter_column("semantic_chunk_embeddings", "chunk_uuid", nullable=True)
    op.create_unique_constraint(
        "uq_semantic_chunk_embeddings_entity_version",
        "semantic_chunk_embeddings",
        ["entity_type", "entity_id", "model", "provider", "model_version", "dimension"],
    )
    op.create_index(
        "ix_semantic_chunk_embeddings_entity_model",
        "semantic_chunk_embeddings",
        ["entity_type", "entity_id", "model", "dimension"],
    )
    op.drop_index(
        "uq_semantic_chunk_embeddings_active_compatibility",
        table_name="semantic_chunk_embeddings",
    )
    op.create_index(
        "uq_semantic_chunk_embeddings_active_compatibility",
        "semantic_chunk_embeddings",
        ["chunk_uuid", "model", "dimension"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE AND chunk_uuid IS NOT NULL"),
    )
    op.create_index(
        "uq_semantic_chunk_embeddings_active_entity",
        "semantic_chunk_embeddings",
        ["entity_type", "entity_id", "model", "dimension"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
    )
    op.execute("UPDATE documents SET needs_revectorize = TRUE WHERE deleted_at IS NULL")
    op.execute(
        "UPDATE files SET needs_revectorize = TRUE "
        "WHERE id IN (SELECT owner_id FROM documents WHERE deleted_at IS NULL)"
    )


def downgrade() -> None:
    op.drop_index(
        "uq_semantic_chunk_embeddings_active_entity",
        table_name="semantic_chunk_embeddings",
    )
    op.drop_index(
        "uq_semantic_chunk_embeddings_active_compatibility",
        table_name="semantic_chunk_embeddings",
    )
    op.create_index(
        "uq_semantic_chunk_embeddings_active_compatibility",
        "semantic_chunk_embeddings",
        ["chunk_uuid", "model", "dimension"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
    )
    op.drop_index(
        "ix_semantic_chunk_embeddings_entity_model",
        table_name="semantic_chunk_embeddings",
    )
    op.drop_constraint(
        "uq_semantic_chunk_embeddings_entity_version",
        "semantic_chunk_embeddings",
        type_="unique",
    )
    op.execute("DELETE FROM semantic_chunk_embeddings WHERE entity_type <> 'semantic_chunk'")
    op.execute("UPDATE semantic_chunk_embeddings SET chunk_uuid = entity_id WHERE chunk_uuid IS NULL")
    op.alter_column("semantic_chunk_embeddings", "chunk_uuid", nullable=False)
    op.drop_column("semantic_chunk_embeddings", "entity_id")
    op.drop_column("semantic_chunk_embeddings", "entity_type")
