"""Add versioned semantic chunk text history and change-log views."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016_semantic_chunk_versions"
down_revision = "0015_entity_embeddings"
branch_labels = None
depends_on = None


CHANGE_LOG_VIEWS = (
    "semantic_chunk_paragraph_change_log",
    "semantic_chunk_document_change_log",
    "semantic_chunk_file_change_log",
)


def _change_log_view_sql(scope: str) -> str:
    scope_column = {
        "paragraph": "sc.paragraph_id",
        "document": "sc.document_id",
        "file": "d.owner_id",
    }[scope]
    return f"""
        CREATE VIEW semantic_chunk_{scope}_change_log AS
        SELECT
            v.id AS version_id,
            v.chunk_uuid,
            v.version_no,
            v.text,
            v.text_sha256,
            v.char_count,
            v.block_meta,
            sc.paragraph_id,
            sc.document_id,
            d.owner_id AS file_id,
            v.created_at,
            v.updated_at,
            lag(v.id) OVER (
                PARTITION BY v.chunk_uuid
                ORDER BY v.version_no
            ) AS previous_version_id,
            lag(v.text_sha256) OVER (
                PARTITION BY v.chunk_uuid
                ORDER BY v.version_no
            ) AS previous_text_sha256
        FROM semantic_chunk_versions AS v
        JOIN semantic_chunks AS sc ON sc.id = v.chunk_uuid
        JOIN documents AS d ON d.id = sc.document_id
        WHERE {scope_column} IS NOT NULL
    """


def upgrade() -> None:
    op.create_table(
        "semantic_chunk_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(length=128), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("block_meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["chunk_uuid"],
            ["semantic_chunks.id"],
            name="fk_semantic_chunk_versions_chunk_uuid_semantic_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_chunk_versions"),
        sa.UniqueConstraint("chunk_uuid", "version_no", name="uq_semantic_chunk_versions_chunk_version"),
        sa.CheckConstraint("version_no > 0", name="semantic_chunk_versions_version_positive"),
        sa.CheckConstraint("char_count >= 0", name="semantic_chunk_versions_char_count_nonnegative"),
    )
    op.create_index(
        "ix_semantic_chunk_versions_chunk_version",
        "semantic_chunk_versions",
        ["chunk_uuid", "version_no"],
    )
    op.create_index(
        "ix_semantic_chunk_versions_text_sha256",
        "semantic_chunk_versions",
        ["text_sha256"],
    )

    op.create_table(
        "semantic_chunk_current",
        sa.Column("chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["chunk_uuid"],
            ["semantic_chunks.id"],
            name="fk_semantic_chunk_current_chunk_uuid_semantic_chunks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["semantic_chunk_versions.id"],
            name="fk_semantic_chunk_current_version_id_semantic_chunk_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_uuid", name="pk_semantic_chunk_current"),
        sa.UniqueConstraint("version_id", name="uq_semantic_chunk_current_version_id"),
    )
    op.create_index(
        "ix_semantic_chunk_current_version_id",
        "semantic_chunk_current",
        ["version_id"],
    )

    op.execute(
        """
        INSERT INTO semantic_chunk_versions
            (chunk_uuid, version_no, text, text_sha256, char_count, block_meta, created_at, updated_at)
        SELECT chunk_uuid, 1, text, text_sha256, char_count, block_meta, created_at, updated_at
        FROM semantic_chunk_texts
        ON CONFLICT (chunk_uuid, version_no) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO semantic_chunk_current (chunk_uuid, version_id)
        SELECT v.chunk_uuid, v.id
        FROM semantic_chunk_versions AS v
        WHERE v.version_no = 1
        ON CONFLICT (chunk_uuid) DO UPDATE
        SET version_id = EXCLUDED.version_id, updated_at = now()
        """
    )

    for scope in ("paragraph", "document", "file"):
        op.execute(_change_log_view_sql(scope))


def downgrade() -> None:
    for view in reversed(CHANGE_LOG_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    op.drop_index("ix_semantic_chunk_current_version_id", table_name="semantic_chunk_current")
    op.drop_table("semantic_chunk_current")
    op.drop_index("ix_semantic_chunk_versions_text_sha256", table_name="semantic_chunk_versions")
    op.drop_index("ix_semantic_chunk_versions_chunk_version", table_name="semantic_chunk_versions")
    op.drop_table("semantic_chunk_versions")
