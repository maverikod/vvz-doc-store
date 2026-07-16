"""Add first-class projects table."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_projects_table"
down_revision = "0005_entity_lifecycle_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("name", name="uq_projects_name"),
    )
    op.create_index("ix_projects_lifecycle", "projects", ["is_deleted", "deleted_at"])

    op.execute(
        """
        INSERT INTO projects (id, name, description)
        WITH source_projects AS (
            SELECT
                documents.block_meta ->> 'project' AS name,
                NULLIF(documents.block_meta ->> 'project_id', '')::uuid AS project_id,
                COALESCE(
                    NULLIF(documents.block_meta ->> 'project_description', ''),
                    documents.block_meta ->> 'project',
                    'Imported project'
                ) AS description
            FROM documents
            WHERE documents.block_meta ? 'project'
              AND NULLIF(documents.block_meta ->> 'project', '') IS NOT NULL
        ),
        ranked_projects AS (
            SELECT
                COALESCE(project_id, gen_random_uuid()) AS id,
                name,
                description,
                row_number() OVER (
                    PARTITION BY name
                    ORDER BY (project_id IS NOT NULL) DESC, project_id NULLS LAST, description
                ) AS row_number
            FROM source_projects
        )
        SELECT id, name, description
        FROM ranked_projects
        WHERE row_number = 1
        ON CONFLICT (name) DO UPDATE
            SET description = EXCLUDED.description,
                updated_at = now()
        """
    )
    op.execute(
        """
        INSERT INTO entity_uuid_registry (entity_table, entity_id)
        SELECT 'projects', id FROM projects
        ON CONFLICT (entity_id) DO UPDATE
            SET entity_table = EXCLUDED.entity_table
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_projects_register_entity_uuid
        AFTER INSERT ON projects
        FOR EACH ROW EXECUTE FUNCTION doc_store_register_entity_uuid()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_projects_unregister_entity_uuid
        AFTER DELETE ON projects
        FOR EACH ROW EXECUTE FUNCTION doc_store_unregister_entity_uuid()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_projects_unregister_entity_uuid ON projects")
    op.execute("DROP TRIGGER IF EXISTS trg_projects_register_entity_uuid ON projects")
    op.execute("DELETE FROM entity_uuid_registry WHERE entity_table = 'projects'")
    op.drop_index("ix_projects_lifecycle", table_name="projects")
    op.drop_table("projects")
