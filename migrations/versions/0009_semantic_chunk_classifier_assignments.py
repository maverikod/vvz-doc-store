"""Add SemanticChunk classifier assignment children."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_semantic_chunk_classifier_assignments"
down_revision = "0008_semantic_chunk_dictionaries"
branch_labels = None
depends_on = None


ASSIGNMENT_TABLES = (
    (
        "semantic_chunk_type_assignments",
        "chunk_type_id",
        "chunk_types",
        "type",
        "DocBlock",
        "fk_scta_chunk",
        "fk_scta_type",
    ),
    (
        "semantic_chunk_role_assignments",
        "role_id",
        "chunk_roles",
        "role",
        "system",
        "fk_scra_chunk",
        "fk_scra_role",
    ),
    (
        "semantic_chunk_status_assignments",
        "status_id",
        "chunk_statuses",
        "status",
        "new",
        "fk_scsa_chunk",
        "fk_scsa_status",
    ),
    (
        "semantic_chunk_block_type_assignments",
        "block_type_id",
        "block_types",
        "block_type",
        "paragraph",
        "fk_scbta_chunk",
        "fk_scbta_block_type",
    ),
    (
        "semantic_chunk_language_assignments",
        "language_id",
        "languages",
        "language",
        "UNKNOWN",
        "fk_scla_chunk",
        "fk_scla_language",
    ),
    (
        "semantic_chunk_category_assignments",
        "category_id",
        "categories",
        "category",
        "uncategorized",
        "fk_scca_chunk",
        "fk_scca_category",
    ),
)


def upgrade() -> None:
    for (
        table,
        column,
        dictionary_table,
        metadata_field,
        default_value,
        chunk_fk_name,
        dictionary_fk_name,
    ) in ASSIGNMENT_TABLES:
        op.create_table(
            table,
            sa.Column("chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(column, postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(
                ["chunk_uuid"],
                ["semantic_chunks.id"],
                name=chunk_fk_name,
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                [column],
                [f"{dictionary_table}.id"],
                name=dictionary_fk_name,
            ),
            sa.PrimaryKeyConstraint("chunk_uuid", name=f"pk_{table}"),
        )
        op.create_index(f"ix_{table}_{column}", table, [column])
        if column == "chunk_type_id":
            value_expression = f"COALESCE(NULLIF(sc.block_meta ->> '{metadata_field}', ''), sc.chunk_type, '{default_value}')"
        else:
            value_expression = f"COALESCE(NULLIF(sc.block_meta ->> '{metadata_field}', ''), '{default_value}')"
        op.execute(
            f"""
            UPDATE semantic_chunks AS sc
            SET {column} = dictionary.id
            FROM {dictionary_table} AS dictionary
            WHERE dictionary.descr = {value_expression}
            """
        )
        op.execute(
            f"""
            INSERT INTO {table} (chunk_uuid, {column})
            SELECT id, {column}
            FROM semantic_chunks
            WHERE {column} IS NOT NULL
            ON CONFLICT (chunk_uuid) DO NOTHING
            """
        )


def downgrade() -> None:
    for (
        table,
        column,
        _dictionary_table,
        _metadata_field,
        _default_value,
        _chunk_fk_name,
        _dictionary_fk_name,
    ) in reversed(ASSIGNMENT_TABLES):
        op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)
