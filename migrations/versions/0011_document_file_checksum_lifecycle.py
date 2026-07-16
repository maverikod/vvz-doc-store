"""Add document/file checksum lifecycle flags.

Revision ID: 0011_document_file_checksum_lifecycle
Revises: 0010_normalize_text_category_default
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_document_file_checksum_lifecycle"
down_revision = "0010_normalize_text_category_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "files",
        sa.Column("needs_revectorize", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "files",
        sa.Column("needs_rechunk", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "documents",
        sa.Column("checksum_algorithm", sa.String(length=32), nullable=False, server_default="sha256"),
    )
    op.add_column("documents", sa.Column("content_sha256", sa.String(length=128), nullable=True))
    op.add_column("documents", sa.Column("body_sha256", sa.String(length=128), nullable=True))
    op.add_column(
        "documents",
        sa.Column("needs_revectorize", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("UPDATE documents SET body_sha256 = source_hash WHERE body_sha256 IS NULL")
    op.execute("UPDATE documents SET content_sha256 = source_hash WHERE content_sha256 IS NULL")
    op.execute("UPDATE documents SET processing_status = 'draft' WHERE processing_status = 'completed'")
    op.create_index("ix_documents_body_sha256", "documents", ["body_sha256"])
    op.create_index("ix_files_reprocessing", "files", ["needs_rechunk", "needs_revectorize"])
    op.create_index("ix_documents_reprocessing", "documents", ["needs_revectorize"])


def downgrade() -> None:
    op.drop_index("ix_documents_reprocessing", table_name="documents")
    op.drop_index("ix_files_reprocessing", table_name="files")
    op.drop_index("ix_documents_body_sha256", table_name="documents")
    op.drop_column("documents", "needs_revectorize")
    op.drop_column("documents", "body_sha256")
    op.drop_column("documents", "content_sha256")
    op.drop_column("documents", "checksum_algorithm")
    op.drop_column("files", "needs_rechunk")
    op.drop_column("files", "needs_revectorize")
