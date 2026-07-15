"""Create the root document hierarchy tables.

This migration mirrors the canonical SQLAlchemy metadata in
``doc_store_server.db.schema``.  Relations owned by later tactical scopes are
intentionally absent.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_hierarchy_chunk_root"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create extensions, then the hierarchy from parents to children."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_upload_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.String(length=2048), nullable=True),
        sa.Column("source_name", sa.String(length=512), nullable=True),
        sa.Column("source_hash", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("processing_attempt", sa.Integer(), nullable=False),
        sa.Column("processing_trace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("block_meta", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint(
            "source_upload_id", "source_version", name="uq_documents_upload_version"
        ),
        sa.CheckConstraint("source_version > 0", name="documents_source_version_positive"),
    )
    op.create_index("ix_documents_source_hash", "documents", ["source_hash"])
    op.create_index("ix_documents_lifecycle", "documents", ["processing_status", "deleted_at"])

    op.create_table(
        "chapters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(length=1024), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("source_start", sa.BigInteger(), nullable=False),
        sa.Column("source_end", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("block_meta", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_chapters_document_id_documents", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chapters"),
        sa.UniqueConstraint("document_id", "order_index", name="uq_chapters_document_order"),
        sa.CheckConstraint("order_index >= 0", name="chapters_order_nonnegative"),
        sa.CheckConstraint(
            "source_start >= 0 AND source_end >= source_start",
            name="chapters_source_range_valid",
        ),
    )
    op.create_index("ix_chapters_document_order", "chapters", ["document_id", "order_index"])

    op.create_table(
        "paragraphs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("source_start", sa.BigInteger(), nullable=False),
        sa.Column("source_end", sa.BigInteger(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("search_weight", sa.Integer(), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("block_meta", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_paragraphs_document_id_documents", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["chapter_id"], ["chapters.id"], name="fk_paragraphs_chapter_id_chapters", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paragraphs"),
        sa.UniqueConstraint("chapter_id", "order_index", name="uq_paragraphs_chapter_order"),
        sa.CheckConstraint("order_index >= 0", name="paragraphs_order_nonnegative"),
        sa.CheckConstraint(
            "source_start >= 0 AND source_end >= source_start",
            name="paragraphs_source_range_valid",
        ),
    )
    op.create_index("ix_paragraphs_chapter_order", "paragraphs", ["chapter_id", "order_index"])
    op.create_index("ix_paragraphs_document_order", "paragraphs", ["document_id", "order_index"])
    op.create_index(
        "ix_paragraphs_search_vector", "paragraphs", ["search_vector"], postgresql_using="gin"
    )

    op.create_table(
        "semantic_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("paragraph_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_start", sa.BigInteger(), nullable=False),
        sa.Column("source_end", sa.BigInteger(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("chunk_type", sa.String(length=64), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("search_weight", sa.Integer(), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("block_meta", postgresql.JSONB(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_semantic_chunks_document_id_documents", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["paragraph_id"], ["paragraphs.id"], name="fk_semantic_chunks_paragraph_id_paragraphs", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["chapter_id"], ["chapters.id"], name="fk_semantic_chunks_chapter_id_chapters", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_chunks"),
        sa.UniqueConstraint(
            "paragraph_id", "order_index", name="uq_semantic_chunks_paragraph_order"
        ),
        sa.CheckConstraint("order_index >= 0", name="semantic_chunks_order_nonnegative"),
        sa.CheckConstraint(
            "source_start >= 0 AND source_end >= source_start",
            name="semantic_chunks_source_range_valid",
        ),
        sa.CheckConstraint("char_count >= 0", name="semantic_chunks_char_count_nonnegative"),
    )
    op.create_index(
        "ix_semantic_chunks_document_order", "semantic_chunks", ["document_id", "order_index"]
    )
    op.create_index(
        "ix_semantic_chunks_paragraph_order", "semantic_chunks", ["paragraph_id", "order_index"]
    )
    op.create_index(
        "ix_semantic_chunks_chapter_order", "semantic_chunks", ["chapter_id", "order_index"]
    )
    op.create_index(
        "ix_semantic_chunks_search_vector",
        "semantic_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    """Drop hierarchy tables from children to parents."""
    op.drop_index("ix_semantic_chunks_search_vector", table_name="semantic_chunks")
    op.drop_index("ix_semantic_chunks_chapter_order", table_name="semantic_chunks")
    op.drop_index("ix_semantic_chunks_paragraph_order", table_name="semantic_chunks")
    op.drop_index("ix_semantic_chunks_document_order", table_name="semantic_chunks")
    op.drop_table("semantic_chunks")

    op.drop_index("ix_paragraphs_search_vector", table_name="paragraphs")
    op.drop_index("ix_paragraphs_document_order", table_name="paragraphs")
    op.drop_index("ix_paragraphs_chapter_order", table_name="paragraphs")
    op.drop_table("paragraphs")

    op.drop_index("ix_chapters_document_order", table_name="chapters")
    op.drop_table("chapters")

    op.drop_index("ix_documents_lifecycle", table_name="documents")
    op.drop_index("ix_documents_source_hash", table_name="documents")
    op.drop_table("documents")
