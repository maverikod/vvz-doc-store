"""Separate semantic chunk text payloads."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0014_semantic_chunk_texts"
down_revision = "0013_owner_id_all_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "semantic_chunk_texts",
        sa.Column("chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(length=128), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("block_meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(
            ["chunk_uuid"],
            ["semantic_chunks.id"],
            name="fk_semantic_chunk_texts_chunk_uuid_semantic_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_uuid", name="pk_semantic_chunk_texts"),
        sa.CheckConstraint("char_count >= 0", name="semantic_chunk_texts_char_count_nonnegative"),
    )
    op.create_index(
        "ix_semantic_chunk_texts_text_sha256",
        "semantic_chunk_texts",
        ["text_sha256"],
    )
    op.execute(
        "CREATE INDEX ix_semantic_chunk_texts_tsvector "
        "ON semantic_chunk_texts USING gin (to_tsvector('simple', COALESCE(text, '')))"
    )
    op.execute(
        """
        INSERT INTO semantic_chunk_texts (chunk_uuid, text, text_sha256, char_count)
        SELECT id, text, encode(digest(text, 'sha256'), 'hex'), char_length(text)
        FROM semantic_chunks
        ON CONFLICT (chunk_uuid) DO NOTHING
        """
    )
    op.execute("UPDATE semantic_chunks SET text = '' WHERE text <> ''")


def downgrade() -> None:
    op.execute(
        """
        UPDATE semantic_chunks AS sc
        SET text = sct.text
        FROM semantic_chunk_texts AS sct
        WHERE sct.chunk_uuid = sc.id
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_semantic_chunk_texts_tsvector")
    op.drop_index("ix_semantic_chunk_texts_text_sha256", table_name="semantic_chunk_texts")
    op.drop_table("semantic_chunk_texts")
