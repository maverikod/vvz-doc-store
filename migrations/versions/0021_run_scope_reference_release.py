"""Let a terminal run release its scope references, as its own foreign keys require.

Migration 0019 declared ``processing_runs.document_id``, ``chapter_id`` and
``chunk_id`` as ``ON DELETE SET NULL`` foreign keys, deliberately: a refused
publication rolls its hierarchy back, so those references must be able to fall
away while the run survives as audit evidence.

The same migration installed ``doc_store_processing_runs_immutable``, whose
first check refuses *any* update to a run whose ``OLD.status`` is terminal,
before it consults which columns changed. Deleting a Document makes PostgreSQL
issue exactly such an update --- ``UPDATE ONLY processing_runs SET document_id =
NULL`` --- so the delete fails with ``restrict_violation`` for every document
that has a succeeded or failed run. The two rules contradicted each other, and
the foreign key lost.

They are not in genuine tension. The three scope references were never part of
``RUN_AUDIT_COLUMNS``: a run's audit identity is what it consumed, under which
deterministic configuration, on whose behalf and when it was recorded. Which
hierarchy rows it still happens to point at is not part of that, which is
precisely why 0019 made them nullable in the first place.

This revision replaces the function so that a terminal run accepts an update
that *releases* scope references --- changes them only from a value to NULL, and
changes nothing else --- and continues to refuse every other rewrite of a closed
run. A release is not a rewrite of history: it records that the rows the run
once pointed at are gone, which is the same fact the null already conveys on a
refused publication.

Only the function body changes. The trigger, the foreign keys, the audit-column
guard and every table shape stay exactly as 0019 left them, so no table's
rendered DDL moves and the parity tests over 0019 remain valid.
"""

from __future__ import annotations

from alembic import op


revision = "0021_run_scope_reference_release"
down_revision = "0020_source_span_diagnostics"
branch_labels = None
depends_on = None


RUN_TABLE = "processing_runs"
RUN_IMMUTABLE_FUNCTION = "doc_store_processing_runs_immutable"

# Exactly the columns 0019 declared ON DELETE SET NULL. A terminal run may
# release these and nothing else.
RUN_SCOPE_REFERENCE_COLUMNS: tuple[str, ...] = ("document_id", "chapter_id", "chunk_id")

# Unchanged from 0019; restated here because this revision rewrites the function
# that reads them and must not silently narrow what it protects.
TERMINAL_RUN_STATUSES: tuple[str, ...] = ("succeeded", "failed", "aborted")

RUN_AUDIT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "primary_source_id",
    "run_kind",
    "comparison_scope",
    "actor",
    "configuration_hash",
    "normalization_profile_id",
    "normalization_profile_version",
    "normalization_parameters",
    "created_at",
)

_TERMINAL_STATUS_SQL = ", ".join(f"'{status}'" for status in TERMINAL_RUN_STATUSES)


def _distinct_from_ladder(columns: tuple[str, ...], indent: str) -> str:
    """Render an IF/ELSIF chain naming the first column a row rewrite touched."""

    lines = []
    for position, column in enumerate(columns):
        keyword = "IF" if position == 0 else "ELSIF"
        lines.append(
            f"{indent}{keyword} NEW.{column} IS DISTINCT FROM OLD.{column} THEN\n"
            f"{indent}    offending_column := '{column}';"
        )
    lines.append(f"{indent}END IF;")
    return "\n".join(lines)


def _released_only_condition(indent: str) -> str:
    """True when this update does nothing but null out scope references.

    Each reference must either be untouched or move from a value to NULL; a
    reference may never be *acquired* or repointed by an update, which would be
    a run claiming it produced something it did not.
    """

    clauses = []
    for column in RUN_SCOPE_REFERENCE_COLUMNS:
        clauses.append(
            f"{indent}    (NEW.{column} IS NOT DISTINCT FROM OLD.{column}\n"
            f"{indent}     OR (OLD.{column} IS NOT NULL AND NEW.{column} IS NULL))"
        )
    return "\n{}    AND\n".format(indent).join(clauses)


def _run_immutable_function_sql() -> str:
    """A terminal run may release scope references; nothing else about it moves."""

    non_reference_columns = tuple(
        column
        for column in _run_row_columns()
        if column not in RUN_SCOPE_REFERENCE_COLUMNS
    )
    return f"""
        CREATE OR REPLACE FUNCTION {RUN_IMMUTABLE_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            offending_column text;
            releasing_scope_only boolean;
        BEGIN
            releasing_scope_only := (
{_released_only_condition(" " * 12)}
            );

{_distinct_from_ladder(non_reference_columns, " " * 12)}

            IF offending_column IS NOT NULL THEN
                releasing_scope_only := false;
            END IF;

            IF OLD.status IN ({_TERMINAL_STATUS_SQL}) AND NOT releasing_scope_only THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = '{RUN_TABLE} is immutable once terminal: run '
                        || OLD.run_id::text
                        || ' already reported status '
                        || OLD.status,
                    HINT = 'Record a new ProcessingRun rather than rewriting a completed '
                        || 'one; a run is the audit evidence for what it produced. '
                        || 'Releasing document_id, chapter_id or chunk_id to NULL is '
                        || 'permitted, because the foreign keys declare it.';
            END IF;

            offending_column := NULL;
{_distinct_from_ladder(RUN_AUDIT_COLUMNS, " " * 12)}

            IF offending_column IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = '{RUN_TABLE} audit identity is immutable: run '
                        || OLD.run_id::text
                        || ' cannot change column '
                        || offending_column,
                    HINT = 'What a run consumed, the deterministic configuration it '
                        || 'applied, the actor it ran for and when it was recorded are '
                        || 'fixed when the run is created.';
            END IF;

            RETURN NEW;
        END
        $$
    """


def _run_row_columns() -> tuple[str, ...]:
    """Every column of ``processing_runs``, read from the ORM declaration.

    Read rather than restated so that a column added later is guarded by default:
    a new column absent from this list would otherwise be silently rewritable on
    a terminal run.
    """

    from doc_store_server.db.schema import ProcessingRun

    return tuple(column.name for column in ProcessingRun.__table__.columns)


def _restore_0019_function_sql() -> str:
    """The 0019 body, verbatim, for downgrade."""

    return f"""
        CREATE OR REPLACE FUNCTION {RUN_IMMUTABLE_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            offending_column text;
        BEGIN
            IF OLD.status IN ({_TERMINAL_STATUS_SQL}) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = '{RUN_TABLE} is immutable once terminal: run '
                        || OLD.run_id::text
                        || ' already reported status '
                        || OLD.status,
                    HINT = 'Record a new ProcessingRun rather than rewriting a completed '
                        || 'one; a run is the audit evidence for what it produced.';
            END IF;

{_distinct_from_ladder(RUN_AUDIT_COLUMNS, " " * 12)}

            IF offending_column IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = '{RUN_TABLE} audit identity is immutable: run '
                        || OLD.run_id::text
                        || ' cannot change column '
                        || offending_column,
                    HINT = 'What a run consumed, the deterministic configuration it '
                        || 'applied, the actor it ran for and when it was recorded are '
                        || 'fixed when the run is created.';
            END IF;

            RETURN NEW;
        END
        $$
    """


def upgrade() -> None:
    op.execute(_run_immutable_function_sql())


def downgrade() -> None:
    op.execute(_restore_0019_function_sql())
