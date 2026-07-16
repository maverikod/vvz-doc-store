"""Normalize legacy inferred text category to the empty category default."""

from __future__ import annotations

from alembic import op


revision = "0010_normalize_text_category_default"
down_revision = "0009_semantic_chunk_classifier_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO categories (id, descr, is_deleted, deleted_at)
        VALUES (gen_random_uuid(), 'uncategorized', FALSE, NULL)
        ON CONFLICT (descr) DO UPDATE SET
            is_deleted = FALSE,
            deleted_at = NULL,
            updated_at = now()
        """
    )
    op.execute(
        """
        WITH category_ids AS (
            SELECT
                (SELECT id FROM categories WHERE descr = 'text') AS text_id,
                (SELECT id FROM categories WHERE descr = 'uncategorized') AS uncategorized_id
        ),
        target_chunks AS (
            SELECT sc.id
            FROM semantic_chunks AS sc, category_ids
            WHERE sc.category_id = category_ids.text_id
              AND sc.block_meta ->> 'category' = 'text'
        )
        UPDATE semantic_chunks AS sc
        SET category_id = category_ids.uncategorized_id,
            block_meta = jsonb_set(
                jsonb_set(
                    jsonb_set(
                        sc.block_meta,
                        '{category}',
                        to_jsonb('uncategorized'::text),
                        true
                    ),
                    '{tags}',
                    COALESCE(
                        (
                            SELECT jsonb_agg(
                                CASE
                                    WHEN value = '"category:text"'::jsonb
                                        THEN '"category:uncategorized"'::jsonb
                                    ELSE value
                                END
                                ORDER BY ordinal
                            )
                            FROM jsonb_array_elements(COALESCE(sc.block_meta -> 'tags', '[]'::jsonb))
                                 WITH ORDINALITY AS tags(value, ordinal)
                        ),
                        '[]'::jsonb
                    ),
                    true
                ),
                '{tags_flat}',
                to_jsonb(replace(COALESCE(sc.block_meta ->> 'tags_flat', ''), 'category:text', 'category:uncategorized')),
                true
            )
        FROM category_ids, target_chunks
        WHERE sc.id = target_chunks.id
        """
    )
    op.execute(
        """
        WITH category_ids AS (
            SELECT
                (SELECT id FROM categories WHERE descr = 'text') AS text_id,
                (SELECT id FROM categories WHERE descr = 'uncategorized') AS uncategorized_id
        )
        UPDATE semantic_chunk_category_assignments AS assignment
        SET category_id = category_ids.uncategorized_id,
            updated_at = now()
        FROM semantic_chunks AS sc, category_ids
        WHERE assignment.chunk_uuid = sc.id
          AND assignment.category_id = category_ids.text_id
          AND sc.category_id = category_ids.uncategorized_id
        """
    )


def downgrade() -> None:
    # The previous value was heuristic data, not user-owned plan or source truth.
    pass
