"""Create the shared bitemporal temporal_assertions table.

Materializes the ``TemporalAssertion`` declaration: one registered version
identity per typed payload claim, linked to the ObjectRegistry boundary rather
than to any typed table, carrying a half-open effective interval and an
immutable transaction time with its acceptance provenance.

History is append-only, and this migration is where that stops being a
convention: ``trg_temporal_assertions_append_only`` refuses any UPDATE that
would rewrite accepted payload evidence, its effective interval, its recorded
transaction time or its acceptance provenance. A correction, an interval
boundary, a deletion or a restoration must be a *new* row naming its
predecessor in ``supersedes_assertion_id``.

The trigger deliberately does not block DELETE. ``object_id`` cascades from the
registry and ``supersedes_assertion_id`` clears itself on delete; hard-delete
governance belongs to a later branch, and vetoing it here would silently break
those declared foreign-key rules. Clearing ``supersedes_assertion_id`` is the
one update the trigger permits, because that is exactly what its own
``ON DELETE SET NULL`` rule performs.

Constraint names are passed through ``conv()`` so PostgreSQL receives the same
identifiers the ORM naming convention emits, including the md5-suffixed
truncation of the 64-character self-referencing foreign-key name.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import conv


revision = "0018_bitemporal_assertions"
down_revision = "0017_semantic_chunk_version_lifecycle"
branch_labels = None
depends_on = None


ASSERTION_KINDS = (
    "payload",
    "interval_boundary",
    "supersession",
    "deletion",
    "restoration",
)

_ASSERTION_KIND_ALLOWLIST_SQL = "assertion_kind IN ({values})".format(
    values=", ".join(f"'{kind}'" for kind in ASSERTION_KINDS)
)

# Every column of an accepted assertion whose value is evidence: identity, the
# object it speaks about, the payload family, the effective interval, the
# transaction time and the acceptance provenance. None of them may ever change.
IMMUTABLE_COLUMNS = (
    "assertion_id",
    "object_id",
    "payload_family",
    "assertion_kind",
    "revision_no",
    "effective_from",
    "effective_until",
    "recorded_at",
    "accepted_by",
    "accepted_via",
    "acceptance_operation_id",
    "acceptance_comment",
)

APPEND_ONLY_FUNCTION = "doc_store_temporal_assertions_append_only"
APPEND_ONLY_TRIGGER = "trg_temporal_assertions_append_only"


def _immutable_column_guards_sql(indent: str) -> str:
    """Render the ELSIF chain that names the first immutable column being rewritten."""

    first, *rest = IMMUTABLE_COLUMNS
    lines = [
        f"IF NEW.{first} IS DISTINCT FROM OLD.{first} THEN",
        f"    offending_column := '{first}';",
    ]
    for column in rest:
        lines.append(f"ELSIF NEW.{column} IS DISTINCT FROM OLD.{column} THEN")
        lines.append(f"    offending_column := '{column}';")
    lines.append("END IF;")
    return "\n".join(f"{indent}{line}" for line in lines).lstrip()


def _append_only_function_sql() -> str:
    return f"""
        CREATE OR REPLACE FUNCTION {APPEND_ONLY_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            offending_column text;
        BEGIN
            {_immutable_column_guards_sql(" " * 12)}

            -- The only permitted update is the ON DELETE SET NULL rule of the
            -- supersession foreign key clearing a link to a removed assertion.
            IF offending_column IS NULL
                AND NEW.supersedes_assertion_id IS DISTINCT FROM OLD.supersedes_assertion_id
                AND NEW.supersedes_assertion_id IS NOT NULL THEN
                offending_column := 'supersedes_assertion_id';
            END IF;

            IF offending_column IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'temporal_assertions is append-only: '
                        || 'assertion ' || OLD.assertion_id::text
                        || ' cannot be rewritten (column ' || offending_column || ')',
                    HINT = 'Append a new assertion whose supersedes_assertion_id names '
                        || 'this one instead of updating accepted evidence.';
            END IF;

            RETURN NEW;
        END
        $$
    """


def _append_only_trigger_sql() -> str:
    return f"""
        CREATE TRIGGER {APPEND_ONLY_TRIGGER}
        BEFORE UPDATE ON temporal_assertions
        FOR EACH ROW EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}()
    """


def upgrade() -> None:
    op.create_table(
        "temporal_assertions",
        sa.Column("assertion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload_family", sa.String(length=64), nullable=False),
        sa.Column(
            "assertion_kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'payload'"),
        ),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("supersedes_assertion_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("accepted_by", sa.String(length=255), nullable=True),
        sa.Column("accepted_via", sa.String(length=64), nullable=True),
        sa.Column("acceptance_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("acceptance_comment", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("assertion_id", name=conv("pk_temporal_assertions")),
        sa.UniqueConstraint(
            "object_id",
            "revision_no",
            name=conv("uq_temporal_assertions_object_revision"),
        ),
        sa.CheckConstraint("revision_no > 0", name=conv("ck_temporal_assertions_revision_positive")),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name=conv("ck_temporal_assertions_effective_interval_half_open"),
        ),
        sa.CheckConstraint(
            _ASSERTION_KIND_ALLOWLIST_SQL,
            name=conv("ck_temporal_assertions_assertion_kind_allowlisted"),
        ),
        sa.CheckConstraint(
            "payload_family <> ''",
            name=conv("ck_temporal_assertions_payload_family_present"),
        ),
        sa.CheckConstraint(
            "supersedes_assertion_id IS NULL OR supersedes_assertion_id <> assertion_id",
            name=conv("ck_temporal_assertions_supersedes_another_assertion"),
        ),
        # The assertion names the RegisteredObject boundary, never a typed table.
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["entity_uuid_registry.entity_id"],
            name=conv("fk_temporal_assertions_object_id_entity_uuid_registry"),
            ondelete="CASCADE",
        ),
        # Non-unique on purpose: a partial-interval correction appends several
        # successors (left copy, corrected overlap, right copy) to one original.
        sa.ForeignKeyConstraint(
            ["supersedes_assertion_id"],
            ["temporal_assertions.assertion_id"],
            name=conv("fk_temporal_assertions_supersedes_assertion_id_temporal_assertions"),
            ondelete="SET NULL",
        ),
    )

    # As-of resolution: effective time, then known time, per registered object.
    op.create_index(
        "ix_temporal_assertions_object_effective",
        "temporal_assertions",
        ["object_id", "effective_from", "effective_until"],
    )
    op.create_index(
        "ix_temporal_assertions_object_recorded",
        "temporal_assertions",
        ["object_id", "recorded_at"],
    )
    op.create_index(
        "ix_temporal_assertions_supersedes",
        "temporal_assertions",
        ["supersedes_assertion_id"],
    )
    op.create_index(
        "ix_temporal_assertions_payload_family",
        "temporal_assertions",
        ["payload_family"],
    )

    op.execute(_append_only_function_sql())
    op.execute(_append_only_trigger_sql())


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {APPEND_ONLY_TRIGGER} ON temporal_assertions")
    op.execute(f"DROP FUNCTION IF EXISTS {APPEND_ONLY_FUNCTION}()")
    op.drop_index("ix_temporal_assertions_payload_family", table_name="temporal_assertions")
    op.drop_index("ix_temporal_assertions_supersedes", table_name="temporal_assertions")
    op.drop_index("ix_temporal_assertions_object_recorded", table_name="temporal_assertions")
    op.drop_index("ix_temporal_assertions_object_effective", table_name="temporal_assertions")
    op.drop_table("temporal_assertions")
