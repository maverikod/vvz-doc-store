"""Add normalized SemanticChunk dictionary references."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from uuid import uuid4


revision = "0008_semantic_chunk_dictionaries"
down_revision = "0007_files_owner_model"
branch_labels = None
depends_on = None


DICTIONARY_TABLES = (
    "chunk_types",
    "chunk_roles",
    "chunk_statuses",
    "block_types",
    "languages",
    "categories",
)

SEMANTIC_CHUNK_REFS = {
    "chunk_type_id": "chunk_types",
    "role_id": "chunk_roles",
    "status_id": "chunk_statuses",
    "block_type_id": "block_types",
    "language_id": "languages",
    "category_id": "categories",
}

SEED_VALUES = {
    "chunk_types": (
        "DocBlock",
        "CodeBlock",
        "Message",
        "Draft",
        "Task",
        "Subtask",
        "TZ",
        "Comment",
        "Log",
        "Metric",
    ),
    "chunk_roles": ("system", "user", "assistant", "tool", "reviewer", "developer"),
    "chunk_statuses": (
        "new",
        "raw",
        "cleaned",
        "verified",
        "validated",
        "reliable",
        "indexed",
        "obsolete",
        "rejected",
        "in_progress",
        "needs_review",
        "archived",
    ),
    "block_types": (
        "paragraph",
        "list",
        "list_item",
        "heading",
        "code_block",
        "quote",
        "table",
        "formula_block",
        "section",
        "message",
        "other",
    ),
    "languages": (
        "UNKNOWN",
        "en",
        "ru",
        "uk",
        "de",
        "fr",
        "es",
        "zh",
        "ja",
        "Assembly",
        "Batchfile",
        "C",
        "C#",
        "C++",
        "Clojure",
        "CMake",
        "COBOL",
        "CoffeeScript",
        "CSS",
        "CSV",
        "Dart",
        "DM",
        "Dockerfile",
        "Elixir",
        "Erlang",
        "Fortran",
        "Go",
        "Groovy",
        "Haskell",
        "HTML",
        "INI",
        "Java",
        "JavaScript",
        "JSON",
        "Julia",
        "Kotlin",
        "Lisp",
        "Lua",
        "Makefile",
        "Markdown",
        "Matlab",
        "Objective-C",
        "OCaml",
        "Pascal",
        "Perl",
        "PHP",
        "PowerShell",
        "Prolog",
        "Python",
        "R",
        "Ruby",
        "Rust",
        "Scala",
        "Shell",
        "SQL",
        "Swift",
        "TeX",
        "TOML",
        "TypeScript",
        "Verilog",
        "Visual Basic",
        "XML",
        "YAML",
        "1C",
        "LaTeX",
        "MathML",
        "AsciiMath",
        "MathJax",
        "KaTeX",
        "SymPy",
    ),
    "categories": ("uncategorized",),
}


def upgrade() -> None:
    for table in DICTIONARY_TABLES:
        op.create_table(
            table,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("descr", sa.String(length=100), nullable=False),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
            sa.UniqueConstraint("descr", name=f"uq_{table}_descr"),
        )
        op.create_index(f"ix_{table}_lifecycle", table, ["is_deleted", "deleted_at"])

    for table, values in SEED_VALUES.items():
        seed_table = sa.table(
            table,
            sa.column("id", postgresql.UUID(as_uuid=True)),
            sa.column("descr", sa.String(length=100)),
        )
        op.bulk_insert(seed_table, [{"id": uuid4(), "descr": value} for value in values])

    for column, table in SEMANTIC_CHUNK_REFS.items():
        op.add_column("semantic_chunks", sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_semantic_chunks_{column}_{table}",
            "semantic_chunks",
            table,
            [column],
            ["id"],
        )
        op.create_index(f"ix_semantic_chunks_{column}", "semantic_chunks", [column])

    op.execute(
        """
        UPDATE semantic_chunks AS sc
        SET chunk_type_id = ct.id
        FROM chunk_types AS ct
        WHERE ct.descr = COALESCE(sc.chunk_type, 'DocBlock')
        """
    )
    op.execute(
        """
        UPDATE semantic_chunks AS sc
        SET role_id = cr.id
        FROM chunk_roles AS cr
        WHERE cr.descr = 'system'
        """
    )
    op.execute(
        """
        UPDATE semantic_chunks AS sc
        SET status_id = cs.id
        FROM chunk_statuses AS cs
        WHERE cs.descr = 'new'
        """
    )
    op.execute(
        """
        UPDATE semantic_chunks AS sc
        SET block_type_id = bt.id
        FROM block_types AS bt
        WHERE bt.descr = 'paragraph'
        """
    )
    op.execute(
        """
        UPDATE semantic_chunks AS sc
        SET language_id = l.id
        FROM languages AS l
        WHERE l.descr = 'UNKNOWN'
        """
    )
    op.execute(
        """
        UPDATE semantic_chunks AS sc
        SET category_id = c.id
        FROM categories AS c
        WHERE c.descr = COALESCE(NULLIF(sc.block_meta ->> 'category', ''), 'uncategorized')
        """
    )

    for table in DICTIONARY_TABLES:
        op.execute(
            f"""
            INSERT INTO entity_uuid_registry (entity_table, entity_id)
            SELECT '{table}', id FROM {table}
            ON CONFLICT (entity_id) DO UPDATE
                SET entity_table = EXCLUDED.entity_table
            """
        )
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
    for table in reversed(DICTIONARY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_unregister_entity_uuid ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_register_entity_uuid ON {table}")
        op.execute(f"DELETE FROM entity_uuid_registry WHERE entity_table = '{table}'")

    for column, table in reversed(tuple(SEMANTIC_CHUNK_REFS.items())):
        op.drop_index(f"ix_semantic_chunks_{column}", table_name="semantic_chunks")
        op.drop_constraint(f"fk_semantic_chunks_{column}_{table}", "semantic_chunks", type_="foreignkey")
        op.drop_column("semantic_chunks", column)

    for table in reversed(DICTIONARY_TABLES):
        op.drop_index(f"ix_{table}_lifecycle", table_name=table)
        op.drop_table(table)
