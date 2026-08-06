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


def _registry_candidate_union_sql() -> str:
    """Union every ENTITY_TABLES member into (entity_id, entity_table) candidate rows."""
    return "\n                    UNION ALL\n".join(
        f"                    SELECT id AS entity_id, '{table}' AS entity_table FROM {table}"
        for table in ENTITY_TABLES
    )


def _legacy_duplicate_preflight_sql() -> str:
    """Abort the migration when one legacy UUID is claimed by more than one typed table.

    Allocation below rejects an already registered UUID instead of reassigning its
    locator, so a pre-existing cross-table duplicate can no longer be silently
    absorbed. The report is ordered by UUID, and by locator inside each UUID, so the
    failure text is deterministic for a given database state.
    """
    return f"""
        DO $$
        DECLARE
            legacy_conflicts text;
        BEGIN
            SELECT string_agg(entity_id::text || ' -> ' || locators, '; ' ORDER BY entity_id)
            INTO legacy_conflicts
            FROM (
                SELECT entity_id, string_agg(entity_table, '+' ORDER BY entity_table) AS locators
                FROM (
{_registry_candidate_union_sql()}
                ) AS registry_candidates
                GROUP BY entity_id
                HAVING count(*) > 1
            ) AS legacy_duplicates;

            IF legacy_conflicts IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'unique_violation',
                    MESSAGE = 'entity_uuid_registry preflight failed: '
                        || 'UUIDs claimed by more than one typed table: '
                        || legacy_conflicts,
                    HINT = 'Give each duplicated row its own UUID, then re-run '
                        || 'migration 0005_entity_lifecycle_registry.';
            END IF;
        END
        $$
    """


def upgrade() -> None:
    op.execute(_legacy_duplicate_preflight_sql())

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

    # The preflight above proved that no UUID is claimed by two typed tables, so this
    # backfill assigns exactly one locator per registered UUID. DO NOTHING keeps the
    # backfill re-runnable without ever reassigning an already registered locator.
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
        DECLARE
            registered_table text;
        BEGIN
            INSERT INTO entity_uuid_registry (entity_table, entity_id)
            VALUES (TG_TABLE_NAME, NEW.id)
            ON CONFLICT (entity_id) DO NOTHING;
            IF NOT FOUND THEN
                SELECT entity_table INTO registered_table
                FROM entity_uuid_registry
                WHERE entity_id = NEW.id;
                RAISE EXCEPTION USING
                    ERRCODE = 'unique_violation',
                    MESSAGE = 'entity_uuid_registry rejected already registered UUID '
                        || NEW.id::text
                        || ' (registered to '
                        || coalesce(registered_table, 'unknown')
                        || ', requested by '
                        || TG_TABLE_NAME
                        || ')',
                    HINT = 'Allocate a fresh UUID; a registered UUID never changes its table.';
            END IF;
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
