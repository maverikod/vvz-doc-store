"""Add lifecycle and lineage metadata to semantic chunk text versions."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0017_semantic_chunk_version_lifecycle"
down_revision = "0016_semantic_chunk_versions"
branch_labels = None
depends_on = None


VERSION_DEPENDENT_TABLES = (
    "semantic_chunk_embeddings",
    "semantic_chunk_tokens",
    "semantic_chunk_tags",
    "semantic_chunk_metrics",
    "semantic_chunk_feedback",
    "semantic_chunk_type_assignments",
    "semantic_chunk_role_assignments",
    "semantic_chunk_status_assignments",
    "semantic_chunk_block_type_assignments",
    "semantic_chunk_language_assignments",
    "semantic_chunk_category_assignments",
)

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
            v.logical_chunk_id,
            v.chunk_uuid,
            v.version_no,
            v.text,
            v.text_sha256,
            v.char_count,
            v.block_meta,
            v.status,
            v.is_current,
            v.valid_from,
            v.valid_to,
            v.comment,
            v.actor,
            v.operation,
            v.operation_id,
            v.previous_version_id,
            v.restored_from_version_id,
            v.source_version_id,
            v.source_start,
            v.source_end,
            v.order_index,
            sc.paragraph_id,
            sc.document_id,
            d.owner_id AS file_id,
            v.created_at,
            v.updated_at,
            lag(v.id) OVER (
                PARTITION BY v.logical_chunk_id
                ORDER BY v.version_no
            ) AS derived_previous_version_id,
            lag(v.text_sha256) OVER (
                PARTITION BY v.logical_chunk_id
                ORDER BY v.version_no
            ) AS previous_text_sha256
        FROM semantic_chunk_versions AS v
        JOIN semantic_chunks AS sc ON sc.id = v.chunk_uuid
        JOIN documents AS d ON d.id = sc.document_id
        WHERE {scope_column} IS NOT NULL
    """


def upgrade() -> None:
    for view in reversed(CHANGE_LOG_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")

    op.add_column(
        "semantic_chunk_versions",
        sa.Column("logical_chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "semantic_chunk_versions",
        sa.Column("previous_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "semantic_chunk_versions",
        sa.Column("restored_from_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("semantic_chunk_versions", sa.Column("source_version_id", sa.String(length=255), nullable=True))
    op.add_column("semantic_chunk_versions", sa.Column("source_start", sa.BigInteger(), nullable=True))
    op.add_column("semantic_chunk_versions", sa.Column("source_end", sa.BigInteger(), nullable=True))
    op.add_column("semantic_chunk_versions", sa.Column("order_index", sa.Integer(), nullable=True))
    op.add_column(
        "semantic_chunk_versions",
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
    )
    op.add_column(
        "semantic_chunk_versions",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "semantic_chunk_versions",
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column("semantic_chunk_versions", sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True))
    op.add_column("semantic_chunk_versions", sa.Column("comment", sa.Text(), nullable=True))
    op.add_column("semantic_chunk_versions", sa.Column("actor", sa.String(length=255), nullable=True))
    op.add_column("semantic_chunk_versions", sa.Column("operation", sa.String(length=64), nullable=True))
    op.add_column("semantic_chunk_versions", sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("semantic_chunk_versions", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    op.execute("UPDATE semantic_chunk_versions SET logical_chunk_id = chunk_uuid WHERE logical_chunk_id IS NULL")
    op.alter_column("semantic_chunk_versions", "logical_chunk_id", nullable=False)
    op.execute(
        """
        UPDATE semantic_chunk_versions AS v
        SET is_current = TRUE,
            status = 'active',
            valid_to = NULL
        FROM semantic_chunk_current AS c
        WHERE c.version_id = v.id
        """
    )
    op.execute(
        """
        UPDATE semantic_chunk_versions
        SET status = 'retired',
            valid_to = COALESCE(updated_at, now())
        WHERE is_current IS FALSE
        """
    )
    op.execute(
        """
        UPDATE semantic_chunk_versions AS v
        SET previous_version_id = p.id
        FROM semantic_chunk_versions AS p
        WHERE p.logical_chunk_id = v.logical_chunk_id
          AND p.version_no = v.version_no - 1
          AND v.previous_version_id IS NULL
        """
    )

    op.create_foreign_key(
        "fk_semantic_chunk_versions_previous_version",
        "semantic_chunk_versions",
        "semantic_chunk_versions",
        ["previous_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_semantic_chunk_versions_restored_from_version",
        "semantic_chunk_versions",
        "semantic_chunk_versions",
        ["restored_from_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_semantic_chunk_versions_logical_version",
        "semantic_chunk_versions",
        ["logical_chunk_id", "version_no"],
    )
    op.create_check_constraint(
        "semantic_chunk_versions_status_valid",
        "semantic_chunk_versions",
        "status IN ('active', 'retired', 'deleted')",
    )
    op.create_index(
        "uq_semantic_chunk_versions_current_logical",
        "semantic_chunk_versions",
        ["logical_chunk_id"],
        unique=True,
        postgresql_where=sa.text("is_current IS TRUE AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_semantic_chunk_versions_logical_status_version",
        "semantic_chunk_versions",
        ["logical_chunk_id", "status", "version_no"],
    )
    op.create_index("ix_semantic_chunk_versions_status", "semantic_chunk_versions", ["status"])
    op.create_index("ix_semantic_chunk_versions_previous", "semantic_chunk_versions", ["previous_version_id"])
    op.create_index("ix_semantic_chunk_versions_operation", "semantic_chunk_versions", ["operation_id"])
    op.create_index("ix_semantic_chunk_versions_source", "semantic_chunk_versions", ["source_version_id"])

    op.add_column("semantic_chunk_current", sa.Column("comment", sa.Text(), nullable=True))
    op.add_column("semantic_chunk_current", sa.Column("actor", sa.String(length=255), nullable=True))
    op.add_column("semantic_chunk_current", sa.Column("operation", sa.String(length=64), nullable=True))

    for table in VERSION_DEPENDENT_TABLES:
        op.add_column(table, sa.Column("chunk_version_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_chunk_version_id",
            table,
            "semantic_chunk_versions",
            ["chunk_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(f"ix_{table}_chunk_version_id", table, ["chunk_version_id"])
        op.execute(
            f"""
            UPDATE {table} AS child
            SET chunk_version_id = c.version_id
            FROM semantic_chunk_current AS c
            WHERE child.chunk_uuid = c.chunk_uuid
              AND child.chunk_version_id IS NULL
            """
        )

    for scope in ("paragraph", "document", "file"):
        op.execute(_change_log_view_sql(scope))


def downgrade() -> None:
    for view in reversed(CHANGE_LOG_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")

    for table in reversed(VERSION_DEPENDENT_TABLES):
        op.drop_index(f"ix_{table}_chunk_version_id", table_name=table)
        op.drop_constraint(f"fk_{table}_chunk_version_id", table, type_="foreignkey")
        op.drop_column(table, "chunk_version_id")

    op.drop_column("semantic_chunk_current", "operation")
    op.drop_column("semantic_chunk_current", "actor")
    op.drop_column("semantic_chunk_current", "comment")

    op.drop_index("ix_semantic_chunk_versions_source", table_name="semantic_chunk_versions")
    op.drop_index("ix_semantic_chunk_versions_operation", table_name="semantic_chunk_versions")
    op.drop_index("ix_semantic_chunk_versions_previous", table_name="semantic_chunk_versions")
    op.drop_index("ix_semantic_chunk_versions_status", table_name="semantic_chunk_versions")
    op.drop_index("ix_semantic_chunk_versions_logical_status_version", table_name="semantic_chunk_versions")
    op.drop_index("uq_semantic_chunk_versions_current_logical", table_name="semantic_chunk_versions")
    op.drop_constraint("semantic_chunk_versions_status_valid", "semantic_chunk_versions", type_="check")
    op.drop_constraint("uq_semantic_chunk_versions_logical_version", "semantic_chunk_versions", type_="unique")
    op.drop_constraint("fk_semantic_chunk_versions_restored_from_version", "semantic_chunk_versions", type_="foreignkey")
    op.drop_constraint("fk_semantic_chunk_versions_previous_version", "semantic_chunk_versions", type_="foreignkey")

    op.drop_column("semantic_chunk_versions", "deleted_at")
    op.drop_column("semantic_chunk_versions", "operation_id")
    op.drop_column("semantic_chunk_versions", "operation")
    op.drop_column("semantic_chunk_versions", "actor")
    op.drop_column("semantic_chunk_versions", "comment")
    op.drop_column("semantic_chunk_versions", "valid_to")
    op.drop_column("semantic_chunk_versions", "valid_from")
    op.drop_column("semantic_chunk_versions", "is_current")
    op.drop_column("semantic_chunk_versions", "status")
    op.drop_column("semantic_chunk_versions", "order_index")
    op.drop_column("semantic_chunk_versions", "source_end")
    op.drop_column("semantic_chunk_versions", "source_start")
    op.drop_column("semantic_chunk_versions", "source_version_id")
    op.drop_column("semantic_chunk_versions", "restored_from_version_id")
    op.drop_column("semantic_chunk_versions", "previous_version_id")
    op.drop_column("semantic_chunk_versions", "logical_chunk_id")

    for scope in ("paragraph", "document", "file"):
        op.execute(_change_log_view_sql(scope))
