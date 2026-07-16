"""Add first-class files and nullable owner links."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_files_owner_model"
down_revision = "0006_projects_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])

    op.create_table(
        "files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("path", sa.String(length=2048), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=True),
        sa.Column("byte_length", sa.BigInteger(), nullable=True),
        sa.Column("char_count", sa.BigInteger(), nullable=True),
        sa.Column("checksum_algorithm", sa.String(length=32), nullable=False, server_default="sha256"),
        sa.Column("content_sha256", sa.String(length=128), nullable=True),
        sa.Column("body_sha256", sa.String(length=128), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("block_meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.PrimaryKeyConstraint("id", name="pk_files"),
        sa.CheckConstraint("checksum_algorithm = 'sha256'", name="files_checksum_algorithm_sha256"),
    )
    op.create_index("ix_files_owner_id", "files", ["owner_id"])
    op.create_index("ix_files_body_sha256", "files", ["body_sha256"])
    op.create_index("ix_files_lifecycle", "files", ["is_deleted", "deleted_at"])

    op.add_column("documents", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])

    op.execute(
        """
        INSERT INTO files (
            id, owner_id, path, name, media_type, byte_length, char_count,
            checksum_algorithm, content_sha256, body_sha256, is_deleted,
            created_at, updated_at, deleted_at, block_meta
        )
        SELECT
            gen_random_uuid(),
            NULL::uuid,
            COALESCE(d.source_path, d.source_name, d.id::text),
            COALESCE(d.source_name, d.source_path, d.id::text),
            'text/plain',
            NULL::bigint,
            NULL::bigint,
            'sha256',
            d.source_hash,
            COALESCE(NULLIF(d.block_meta ->> 'body_sha256', ''), d.source_hash, repeat('0', 64)),
            d.is_deleted,
            d.created_at,
            d.updated_at,
            d.deleted_at,
            jsonb_strip_nulls(
                jsonb_build_object(
                    'migrated_from_document_id', d.id::text,
                    'source_version_id', d.block_meta ->> 'source_version_id',
                    'source_path', d.source_path,
                    'source_name', d.source_name
                )
            )
        FROM documents AS d
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE documents AS d
        SET owner_id = f.id
        FROM files AS f
        WHERE f.block_meta ->> 'migrated_from_document_id' = d.id::text
          AND d.owner_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE documents
        SET block_meta = block_meta || jsonb_build_object('file_id', owner_id::text)
        WHERE owner_id IS NOT NULL
        """
    )

    op.execute(
        """
        INSERT INTO entity_uuid_registry (entity_table, entity_id)
        SELECT 'files', id FROM files
        ON CONFLICT (entity_id) DO UPDATE
            SET entity_table = EXCLUDED.entity_table
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_files_register_entity_uuid
        AFTER INSERT ON files
        FOR EACH ROW EXECUTE FUNCTION doc_store_register_entity_uuid()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_files_unregister_entity_uuid
        AFTER DELETE ON files
        FOR EACH ROW EXECUTE FUNCTION doc_store_unregister_entity_uuid()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_files_unregister_entity_uuid ON files")
    op.execute("DROP TRIGGER IF EXISTS trg_files_register_entity_uuid ON files")
    op.execute("DELETE FROM entity_uuid_registry WHERE entity_table = 'files'")
    op.drop_index("ix_documents_owner_id", table_name="documents")
    op.drop_column("documents", "owner_id")
    op.drop_index("ix_files_lifecycle", table_name="files")
    op.drop_index("ix_files_body_sha256", table_name="files")
    op.drop_index("ix_files_owner_id", table_name="files")
    op.drop_table("files")
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_column("projects", "owner_id")
