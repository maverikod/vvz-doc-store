"""Create ordered semantic-chunk token and tag mappings."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_chunk_tokens_tags"
down_revision = "0001_hierarchy_chunk_root"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create normalized token and tag children of semantic chunks."""
    op.create_table(
        "semantic_chunk_tokens",
        sa.Column("chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_kind", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("token_value", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_uuid"],
            ["semantic_chunks.id"],
            name="fk_semantic_chunk_tokens_chunk_uuid_semantic_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "chunk_uuid", "token_kind", "ordinal", name="pk_semantic_chunk_tokens"
        ),
        sa.UniqueConstraint(
            "chunk_uuid",
            "token_kind",
            "ordinal",
            name="uq_semantic_chunk_tokens_identity",
        ),
        sa.CheckConstraint(
            "token_kind IN ('tokens', 'bm25_tokens')",
            name="semantic_chunk_tokens_kind_valid",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="semantic_chunk_tokens_ordinal_nonnegative"
        ),
    )
    op.create_index(
        "ix_semantic_chunk_tokens_chunk_kind_ordinal",
        "semantic_chunk_tokens",
        ["chunk_uuid", "token_kind", "ordinal"],
    )
    op.create_index(
        "ix_semantic_chunk_tokens_kind_value",
        "semantic_chunk_tokens",
        ["token_kind", "token_value"],
    )

    op.create_table(
        "semantic_chunk_tags",
        sa.Column("chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("tag_value", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_uuid"],
            ["semantic_chunks.id"],
            name="fk_semantic_chunk_tags_chunk_uuid_semantic_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "chunk_uuid", "ordinal", name="pk_semantic_chunk_tags"
        ),
        sa.UniqueConstraint(
            "chunk_uuid", "ordinal", name="uq_semantic_chunk_tags_identity"
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="semantic_chunk_tags_ordinal_nonnegative"
        ),
    )
    op.create_index(
        "ix_semantic_chunk_tags_chunk_ordinal",
        "semantic_chunk_tags",
        ["chunk_uuid", "ordinal"],
    )
    op.create_index(
        "ix_semantic_chunk_tags_value", "semantic_chunk_tags", ["tag_value"]
    )


def downgrade() -> None:
    """Drop token and tag mappings while preserving semantic chunks."""
    op.drop_index(
        "ix_semantic_chunk_tags_value", table_name="semantic_chunk_tags"
    )
    op.drop_index(
        "ix_semantic_chunk_tags_chunk_ordinal", table_name="semantic_chunk_tags"
    )
    op.drop_table("semantic_chunk_tags")

    op.drop_index(
        "ix_semantic_chunk_tokens_kind_value", table_name="semantic_chunk_tokens"
    )
    op.drop_index(
        "ix_semantic_chunk_tokens_chunk_kind_ordinal",
        table_name="semantic_chunk_tokens",
    )
    op.drop_table("semantic_chunk_tokens")
