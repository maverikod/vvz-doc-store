"""Align the deployed ObjectRegistry DDL with the ``EntityUuidRegistry`` declaration.

Migration ``0005_entity_lifecycle_registry`` created ``entity_uuid_registry``
with the composite primary key ``(entity_table, entity_id)`` and no ``kind``
column at all, while ``doc_store_server.db.schema.EntityUuidRegistry`` declares a
single-column ``entity_id`` primary key plus a required allowlisted ``kind`` and
both allowlist check constraints. The committed object model therefore could not
be deployed. This migration closes that gap in place, without touching 0005 or
any other migration: the registry is extended, never replaced, and no parallel
registry is introduced ({vq2e}).

What it establishes:

* {vi72}/{ss45} one UUIDv4 is the whole identity of a registered object, so
  ``entity_id`` becomes the sole primary key and the UUID alone resolves the row
  without a caller-supplied type. ``uq_entity_uuid_registry_entity_id`` and
  ``ix_entity_uuid_registry_entity_table`` are deliberately left in place: the
  unique constraint is what ``temporal_assertions.object_id`` references and what
  ``ON CONFLICT (entity_id)`` arbitrates against, and the index still serves
  locator-scoped scans.
* {dl1d} every row carries an allowlisted ``kind`` next to its concrete typed
  table locator, enforced by ``ck_entity_uuid_registry_kind_allowlisted`` over
  ``kind`` and ``ck_entity_uuid_registry_kind_locator_allowlisted`` over the
  ``(kind, entity_table)`` pair. Both predicates are generated from the same
  ``REGISTRY_KIND_LOCATORS`` mapping the ORM uses, so the DDL that reaches the
  database can never be a stale copy of the allowlist.
* {b2v7} ``doc_store_register_entity_uuid`` now supplies ``kind`` together with
  ``entity_table``, resolved from ``TG_TABLE_NAME`` through the same allowlist,
  while preserving the ``ON CONFLICT (entity_id) DO NOTHING`` plus ``NOT FOUND``
  duplicate rejection established for the registry allocation path. A table that
  is not allowlisted is refused outright rather than registered without a kind.

The backfill derives ``kind`` from the locator already stored on each row. A
locator outside the allowlist is not guessed at and not defaulted: an allowlist
preflight raises before any DDL runs, naming every offending ``entity_table``
value in a deterministic, alphabetically ordered report, so the migration either
aligns the whole registry or changes nothing.

Revision identifiers in this repository are full file slugs and migrations are
ordered by ``down_revision`` through
``doc_store_server.db.migrations.apply_migrations``; there is no alembic CLI.
The ``0018a`` suffix therefore inserts this revision after ``0018`` without
renumbering any successor, and it sorts between ``0018_`` and ``0018b_`` for the
``sorted(glob("*.py"))`` load the runner performs.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql.elements import conv

from doc_store_server.db.schema import REGISTRY_KIND_LOCATORS


revision = "0018a_object_registry_kind_alignment"
down_revision = "0018_bitemporal_assertions"
branch_labels = None
depends_on = None


REGISTRY_TABLE = "entity_uuid_registry"
PRIMARY_KEY = "pk_entity_uuid_registry"
KIND_CHECK = "ck_entity_uuid_registry_kind_allowlisted"
KIND_LOCATOR_CHECK = "ck_entity_uuid_registry_kind_locator_allowlisted"
REGISTER_FUNCTION = "doc_store_register_entity_uuid"

# The composite key 0005 created, restored verbatim by the downgrade.
LEGACY_PRIMARY_KEY_COLUMNS = ("entity_table", "entity_id")

# Every allowlist predicate below is rendered from this one mapping, which is the
# same object the ORM declaration reads, so the migration cannot drift from it.
_KIND_ALLOWLIST_SQL = "kind IN ({values})".format(
    values=", ".join(f"'{kind}'" for kind in REGISTRY_KIND_LOCATORS)
)
_KIND_LOCATOR_ALLOWLIST_SQL = "(kind, entity_table) IN ({pairs})".format(
    pairs=", ".join(f"('{kind}', '{table}')" for kind, table in REGISTRY_KIND_LOCATORS.items())
)


def _allowlist_values_sql() -> str:
    """Render the allowlist as a ``VALUES`` list of ``(kind, entity_table)`` rows."""

    return ", ".join(f"('{kind}', '{table}')" for kind, table in REGISTRY_KIND_LOCATORS.items())


def _allowlisted_locators_sql() -> str:
    """Render the allowlisted typed-table locators as an ``IN`` list."""

    return ", ".join(f"'{table}'" for table in REGISTRY_KIND_LOCATORS.values())


def _locator_allowlist_preflight_sql() -> str:
    """Abort before any DDL when a registry row names a locator the allowlist rejects.

    The backfill can only derive ``kind`` from ``entity_table``; a locator that is
    not allowlisted has no kind to derive, and inventing one would smuggle an
    unregistered family past the check constraints. The report is DISTINCT and
    alphabetically ordered so the failure text is deterministic for a given
    database state.
    """

    return f"""
        DO $$
        DECLARE
            unknown_locators text;
        BEGIN
            SELECT string_agg(DISTINCT entity_table, ', ' ORDER BY entity_table)
            INTO unknown_locators
            FROM {REGISTRY_TABLE}
            WHERE entity_table NOT IN ({_allowlisted_locators_sql()});

            IF unknown_locators IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = '{REGISTRY_TABLE} kind backfill failed: '
                        || 'rows registered to locators outside the allowlist: '
                        || unknown_locators,
                    HINT = 'Add the missing kind to REGISTRY_KIND_LOCATORS, or remove the '
                        || 'rows, then re-run migration '
                        || '0018a_object_registry_kind_alignment.';
            END IF;
        END
        $$
    """


def _kind_backfill_sql() -> str:
    """Derive ``kind`` for every existing row from its already stored locator."""

    return f"""
        UPDATE {REGISTRY_TABLE} AS registry
        SET kind = allowlist.kind
        FROM (VALUES {_allowlist_values_sql()}) AS allowlist(kind, entity_table)
        WHERE registry.entity_table = allowlist.entity_table
          AND registry.kind IS DISTINCT FROM allowlist.kind
    """


def _aligned_register_function_sql() -> str:
    """Registration that supplies ``kind`` and still refuses an already registered UUID."""

    return f"""
        CREATE OR REPLACE FUNCTION {REGISTER_FUNCTION}()
        RETURNS trigger AS $$
        DECLARE
            registered_table text;
            allocated_kind text;
        BEGIN
            SELECT allowlist.kind INTO allocated_kind
            FROM (VALUES {_allowlist_values_sql()}) AS allowlist(kind, entity_table)
            WHERE allowlist.entity_table = TG_TABLE_NAME;

            IF allocated_kind IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = '{REGISTRY_TABLE} rejected unallowlisted table '
                        || TG_TABLE_NAME
                        || ' for UUID '
                        || NEW.id::text,
                    HINT = 'Every registered object needs an allowlisted kind; add the '
                        || 'table to REGISTRY_KIND_LOCATORS before registering it.';
            END IF;

            INSERT INTO {REGISTRY_TABLE} (kind, entity_table, entity_id)
            VALUES (allocated_kind, TG_TABLE_NAME, NEW.id)
            ON CONFLICT (entity_id) DO NOTHING;
            IF NOT FOUND THEN
                SELECT entity_table INTO registered_table
                FROM {REGISTRY_TABLE}
                WHERE entity_id = NEW.id;
                RAISE EXCEPTION USING
                    ERRCODE = 'unique_violation',
                    MESSAGE = '{REGISTRY_TABLE} rejected already registered UUID '
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


def _legacy_register_function_sql() -> str:
    """The pre-``kind`` registration body established by 0005, restored on downgrade."""

    return f"""
        CREATE OR REPLACE FUNCTION {REGISTER_FUNCTION}()
        RETURNS trigger AS $$
        DECLARE
            registered_table text;
        BEGIN
            INSERT INTO {REGISTRY_TABLE} (entity_table, entity_id)
            VALUES (TG_TABLE_NAME, NEW.id)
            ON CONFLICT (entity_id) DO NOTHING;
            IF NOT FOUND THEN
                SELECT entity_table INTO registered_table
                FROM {REGISTRY_TABLE}
                WHERE entity_id = NEW.id;
                RAISE EXCEPTION USING
                    ERRCODE = 'unique_violation',
                    MESSAGE = '{REGISTRY_TABLE} rejected already registered UUID '
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


def upgrade() -> None:
    # Nothing is altered until every stored locator is known to have a kind.
    op.execute(_locator_allowlist_preflight_sql())

    op.add_column(REGISTRY_TABLE, sa.Column("kind", sa.String(length=64), nullable=True))
    op.execute(_kind_backfill_sql())
    op.alter_column(
        REGISTRY_TABLE,
        "kind",
        existing_type=sa.String(length=64),
        nullable=False,
    )

    # The UUID alone is the identity: the locator stops being part of the key.
    op.drop_constraint(PRIMARY_KEY, REGISTRY_TABLE, type_="primary")
    op.create_primary_key(conv(PRIMARY_KEY), REGISTRY_TABLE, ["entity_id"])

    op.create_check_constraint(conv(KIND_CHECK), REGISTRY_TABLE, _KIND_ALLOWLIST_SQL)
    op.create_check_constraint(
        conv(KIND_LOCATOR_CHECK), REGISTRY_TABLE, _KIND_LOCATOR_ALLOWLIST_SQL
    )

    op.execute(_aligned_register_function_sql())


def downgrade() -> None:
    op.execute(_legacy_register_function_sql())

    op.drop_constraint(KIND_LOCATOR_CHECK, REGISTRY_TABLE, type_="check")
    op.drop_constraint(KIND_CHECK, REGISTRY_TABLE, type_="check")

    op.drop_constraint(PRIMARY_KEY, REGISTRY_TABLE, type_="primary")
    op.create_primary_key(conv(PRIMARY_KEY), REGISTRY_TABLE, list(LEGACY_PRIMARY_KEY_COLUMNS))

    op.drop_column(REGISTRY_TABLE, "kind")
