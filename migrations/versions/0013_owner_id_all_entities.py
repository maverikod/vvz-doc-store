"""Add owner_id to every addressable entity table."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013_owner_id_all_entities"
down_revision = "0012_embedding_vector_dimension_384"
branch_labels = None
depends_on = None


OWNER_TABLES = (
    "chunk_types",
    "chunk_roles",
    "chunk_statuses",
    "block_types",
    "languages",
    "categories",
    "chapters",
    "paragraphs",
    "semantic_chunks",
)


def upgrade() -> None:
    for table in OWNER_TABLES:
        op.add_column(table, sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])

    op.execute("UPDATE chapters SET owner_id = document_id WHERE owner_id IS NULL")
    op.execute("UPDATE paragraphs SET owner_id = chapter_id WHERE owner_id IS NULL")
    op.execute("UPDATE semantic_chunks SET owner_id = paragraph_id WHERE owner_id IS NULL")


def downgrade() -> None:
    for table in reversed(OWNER_TABLES):
        op.drop_index(f"ix_{table}_owner_id", table_name=table)
        op.drop_column(table, "owner_id")
