"""Create chunk links, versioned embeddings, and promoted block metadata."""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql


revision = "0004_chunk_links_embeddings_metadata"
down_revision = "0001_hierarchy_chunk_root"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create link and embedding children, then promote known chunk metadata."""
    op.create_table(
        "semantic_chunk_links",
        sa.Column("source_chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("target_chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "relation_data",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["source_chunk_uuid"],
            ["semantic_chunks.id"],
            name="fk_semantic_chunk_links_source_chunk_uuid_semantic_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "source_chunk_uuid", "relation_type", "target_chunk_uuid", "ordinal",
            name="pk_semantic_chunk_links",
        ),
        sa.UniqueConstraint(
            "source_chunk_uuid",
            "relation_type",
            "target_chunk_uuid",
            "ordinal",
            name="uq_semantic_chunk_links_ordered_identity",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="semantic_chunk_links_ordinal_nonnegative"
        ),
    )
    op.create_index(
        "ix_semantic_chunk_links_source_type",
        "semantic_chunk_links",
        ["source_chunk_uuid", "relation_type"],
    )
    op.create_index(
        "ix_semantic_chunk_links_target",
        "semantic_chunk_links",
        ["target_chunk_uuid"],
    )

    op.create_table(
        "semantic_chunk_embeddings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vector", VECTOR(384), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(
            ["chunk_uuid"],
            ["semantic_chunks.id"],
            name="fk_semantic_chunk_embeddings_chunk_uuid_semantic_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_chunk_embeddings"),
        sa.UniqueConstraint(
            "chunk_uuid",
            "model",
            "provider",
            "model_version",
            "dimension",
            name="uq_semantic_chunk_embeddings_version",
        ),
        sa.CheckConstraint(
            "dimension > 0", name="semantic_chunk_embeddings_dimension_positive"
        ),
    )
    op.create_index(
        "ix_semantic_chunk_embeddings_chunk_model",
        "semantic_chunk_embeddings",
        ["chunk_uuid", "model", "dimension"],
    )
    op.create_index(
        "ix_semantic_chunk_embeddings_vector_cosine",
        "semantic_chunk_embeddings",
        ["vector"],
        postgresql_using="hnsw",
        postgresql_ops={"vector": "vector_cosine_ops"},
    )
    op.create_index(
        "uq_semantic_chunk_embeddings_active_compatibility",
        "semantic_chunk_embeddings",
        ["chunk_uuid", "model", "dimension"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
    )

    op.add_column(
        "semantic_chunks",
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "semantic_chunks",
        sa.Column("parent_type", sa.String(length=64), nullable=True),
    )
    op.add_column("semantic_chunks", sa.Column("markup", sa.Text(), nullable=True))
    op.add_column(
        "semantic_chunks", sa.Column("list_level", sa.Integer(), nullable=True)
    )
    op.add_column(
        "semantic_chunks", sa.Column("heading_level", sa.Integer(), nullable=True)
    )
    op.add_column(
        "semantic_chunks", sa.Column("aggregation", postgresql.JSONB(), nullable=True)
    )
    op.create_index(
        "ix_semantic_chunks_parent_type_order",
        "semantic_chunks",
        ["parent_id", "parent_type", "order_index"],
    )


def downgrade() -> None:
    """Remove promoted metadata and child tables, preserving the root schema."""
    op.drop_index(
        "ix_semantic_chunks_parent_type_order", table_name="semantic_chunks"
    )
    op.drop_column("semantic_chunks", "aggregation")
    op.drop_column("semantic_chunks", "heading_level")
    op.drop_column("semantic_chunks", "list_level")
    op.drop_column("semantic_chunks", "markup")
    op.drop_column("semantic_chunks", "parent_type")
    op.drop_column("semantic_chunks", "parent_id")

    op.drop_index(
        "uq_semantic_chunk_embeddings_active_compatibility",
        table_name="semantic_chunk_embeddings",
    )
    op.drop_index(
        "ix_semantic_chunk_embeddings_vector_cosine",
        table_name="semantic_chunk_embeddings",
    )
    op.drop_index(
        "ix_semantic_chunk_embeddings_chunk_model",
        table_name="semantic_chunk_embeddings",
    )
    op.drop_table("semantic_chunk_embeddings")

    op.drop_index("ix_semantic_chunk_links_target", table_name="semantic_chunk_links")
    op.drop_index(
        "ix_semantic_chunk_links_source_type", table_name="semantic_chunk_links"
    )
    op.drop_table("semantic_chunk_links")
