"""Add addressable entity lifecycle registry and deletion markers."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_entity_lifecycle_registry"
down_revision = "0001_hierarchy_chunk_root"
branch_labels = None
depends_on = None


ENTITY_TABLES = ("documents", "chapters", "paragraphs", "semantic_chunks")


def upgrade() -> None:
    for table in ENTITY_TABLES:
        op.add_column(
            table,
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
        op.execute(
            sa.text(
                f"UPDATE {table} SET is_deleted = TRUE WHERE deleted_at IS NOT NULL"
            )
        )
        op.create_index(f"ix_{table}_is_deleted", table, ["is_deleted"])

    op.create_table(
        "entity_uuid_registry",
        sa.Column("entity_table", sa.String(length=128), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("entity_table", "entity_id", name="pk_entity_uuid_registry"),
        sa.UniqueConstraint("entity_id", name="uq_entity_uuid_registry_entity_id"),
    )
    op.create_index(
        "ix_entity_uuid_registry_entity_table",
        "entity_uuid_registry",
        ["entity_table"],
    )

    for table in ENTITY_TABLES:
        op.execute(
            sa.text(
                "INSERT INTO entity_uuid_registry (entity_table, entity_id) "
                f"SELECT :table_name, id FROM {table} "
                "ON CONFLICT (entity_id) DO NOTHING"
            ).bindparams(table_name=table)
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION doc_store_register_entity_uuid()
        RETURNS trigger AS $$
        BEGIN
            INSERT INTO entity_uuid_registry (entity_table, entity_id)
            VALUES (TG_TABLE_NAME, NEW.id)
            ON CONFLICT (entity_id) DO UPDATE
                SET entity_table = EXCLUDED.entity_table;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION doc_store_unregister_entity_uuid()
        RETURNS trigger AS $$
        BEGIN
            DELETE FROM entity_uuid_registry
            WHERE entity_table = TG_TABLE_NAME AND entity_id = OLD.id;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ENTITY_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_register_entity_uuid
            AFTER INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION doc_store_register_entity_uuid()
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_unregister_entity_uuid
            AFTER DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION doc_store_unregister_entity_uuid()
            """
        )


def downgrade() -> None:
    for table in ENTITY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_unregister_entity_uuid ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_register_entity_uuid ON {table}")
    op.execute("DROP FUNCTION IF EXISTS doc_store_unregister_entity_uuid()")
    op.execute("DROP FUNCTION IF EXISTS doc_store_register_entity_uuid()")
    op.drop_index("ix_entity_uuid_registry_entity_table", table_name="entity_uuid_registry")
    op.drop_table("entity_uuid_registry")
    for table in reversed(ENTITY_TABLES):
        op.drop_index(f"ix_{table}_is_deleted", table_name=table)
        op.drop_column(table, "is_deleted")
