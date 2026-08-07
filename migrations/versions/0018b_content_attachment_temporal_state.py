"""Bind ContentAttachment activity to the one ``TemporalAssertion`` identity.

A ContentAttachment is an independently referenceable directed attachment
({5c7n}); whether it is *active* is a claim about a registered object, and this
migration says that claim in the only vocabulary the store has for claims: an
append-only temporal assertion.

No second version system is created ({j7vu}/{ID08}). ``content_attachment_states``
is a *family payload table keyed by the assertion UUID*: it holds no identity of
its own, no revision counter, no validity interval and no current-state pointer.
Every temporal property of an attachment state — its half-open effective
interval ({tbfi}), its immutable transaction time and acceptance provenance
({405f}), its monotonic revision order ({8nd5}) and its supersession chain
({4jxy}) — is already carried by the ``temporal_assertions`` row this payload
hangs from, which in turn names the RegisteredObject directly ({ss96}). The
payload adds exactly one fact the shared model cannot express: *was the
attachment active over that interval*.

How the three activity transitions are said, and how each stays append-only
({1hst}/{xmk2}):

* **creation** appends a ``payload`` assertion whose state row is active;
* **deactivation** appends a ``deletion`` assertion whose state row is inactive.
  The creating assertion is neither rewritten nor removed, so a query as-of an
  earlier known time still sees the attachment active;
* **restoration** appends a ``restoration`` assertion whose state row is active
  again, superseding the deactivation without erasing it, so a known time
  between the two still resolves to inactive.

``trg_content_attachment_states_guard`` is what makes those three sentences
true at the database boundary rather than by convention: an inactive
``restoration`` and an active ``deletion`` are both refused, the payload may
only hang from an assertion in the ``content_attachment`` family, and that
assertion may only speak about an object the ObjectRegistry registered with kind
``content_attachment`` (G-001). ``trg_content_attachment_states_append_only``
then mirrors the guarantee ``0018_bitemporal_assertions`` gives the assertion
row: an accepted state is never rewritten, only superseded by a new one.

``doc_store_content_attachment_state_as_of`` is the read half. It resolves the
single attachment state visible at one ``(act_date, known_at)`` pair — effective
interval contains ``act_date``, ``recorded_at`` is at or before ``known_at``,
and no assertion recorded by ``known_at`` supersedes it — and both parameters
default to PostgreSQL ``now()``, which is the transaction timestamp and is
therefore resolved once at request start ({1vse}/{BT05}). Ambiguity is rejected
rather than arbitrated: two simultaneously accepted states over one effective
instant raise ``cardinality_violation`` naming both assertions ({8ysr}/{BT10}).
The predicate itself lives once, in ``doc_store_content_attachment_state_visible``,
so the ambiguity check and the returned row can never drift apart.

Deliberately out of scope, and owned elsewhere: the ``content_attachments``
table and the endpoint columns of an attachment, endpoint kind restriction,
self-attachment and duplicate-active-edge rejection, mixed-kind graph cycle
detection, File payload behaviour, and any lifecycle or public command. The
attachment locator is already allowlisted in ``REGISTRY_KIND_LOCATORS`` ahead of
its table, which is precisely what lets this migration bind attachment identity
to the registry without owning the typed table.

Revision identifiers in this repository are full file slugs, and migrations are
ordered by ``down_revision`` through
``doc_store_server.db.migrations.apply_migrations``; there is no alembic CLI.
The ``0018b`` suffix therefore inserts this revision after ``0018a`` without
renumbering any successor, and it sorts after ``0018a_`` for the
``sorted(glob("*.py"))`` load the runner performs.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import conv


revision = "0018b_content_attachment_temporal_state"
down_revision = "0018a_object_registry_kind_alignment"
branch_labels = None
depends_on = None


ASSERTION_TABLE = "temporal_assertions"
REGISTRY_TABLE = "entity_uuid_registry"
STATE_TABLE = "content_attachment_states"

# The family name on the shared assertion, and the registry kind of the object
# it may speak about. They are the same word because the payload family *is* the
# attachment kind; nothing here invents a second namespace.
PAYLOAD_FAMILY = "content_attachment"
REGISTRY_KIND = "content_attachment"

# The activity transitions, expressed as shared assertion kinds. Both already
# exist in the 0018 allowlist; this migration only binds their meaning for one
# payload family.
DEACTIVATION_KIND = "deletion"
RESTORATION_KIND = "restoration"

STATE_GUARD_FUNCTION = "doc_store_content_attachment_state_guard"
STATE_GUARD_TRIGGER = "trg_content_attachment_states_guard"
APPEND_ONLY_FUNCTION = "doc_store_content_attachment_states_append_only"
APPEND_ONLY_TRIGGER = "trg_content_attachment_states_append_only"
VISIBLE_FUNCTION = "doc_store_content_attachment_state_visible"
AS_OF_FUNCTION = "doc_store_content_attachment_state_as_of"

# One row shape for both resolution functions, so the ambiguity check and the
# row it guards are literally the same projection.
_RESOLUTION_COLUMNS = """
            assertion_id uuid,
            object_id uuid,
            revision_no integer,
            assertion_kind text,
            is_active boolean,
            effective_from timestamptz,
            effective_until timestamptz,
            recorded_at timestamptz
"""


def _state_guard_function_sql() -> str:
    """Refuse a payload that would misreport the family, the object or the transition."""

    return f"""
        CREATE OR REPLACE FUNCTION {STATE_GUARD_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            asserted record;
            registered_kind text;
        BEGIN
            SELECT a.object_id, a.payload_family, a.assertion_kind
            INTO asserted
            FROM {ASSERTION_TABLE} AS a
            WHERE a.assertion_id = NEW.assertion_id;

            IF NOT FOUND THEN
                -- The foreign key is an AFTER-row constraint trigger and has not
                -- fired yet. Let it report the missing assertion itself rather
                -- than masking a referential error with a family error.
                RETURN NEW;
            END IF;

            IF asserted.payload_family <> '{PAYLOAD_FAMILY}' THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = '{STATE_TABLE} rejected assertion '
                        || NEW.assertion_id::text
                        || ': payload family is '
                        || asserted.payload_family
                        || ', not {PAYLOAD_FAMILY}',
                    HINT = 'A family payload table may only key assertions of its own '
                        || 'payload family.';
            END IF;

            SELECT r.kind INTO registered_kind
            FROM {REGISTRY_TABLE} AS r
            WHERE r.entity_id = asserted.object_id;

            IF registered_kind IS DISTINCT FROM '{REGISTRY_KIND}' THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = '{STATE_TABLE} rejected assertion '
                        || NEW.assertion_id::text
                        || ': registered object '
                        || asserted.object_id::text
                        || ' has kind '
                        || coalesce(registered_kind, 'unregistered')
                        || ', not {REGISTRY_KIND}',
                    HINT = 'Attachment state may only be asserted about an object the '
                        || 'ObjectRegistry registered as a {REGISTRY_KIND}.';
            END IF;

            -- Deactivation and restoration are not free-form payloads: the shared
            -- assertion kind and the asserted activity must agree, or the history
            -- would read as a restoration that restores nothing.
            IF asserted.assertion_kind = '{DEACTIVATION_KIND}' AND NEW.is_active THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = '{STATE_TABLE} rejected assertion '
                        || NEW.assertion_id::text
                        || ': a {DEACTIVATION_KIND} assertion must state is_active = false',
                    HINT = 'Deactivate by appending a {DEACTIVATION_KIND} assertion whose '
                        || 'state is inactive.';
            END IF;

            IF asserted.assertion_kind = '{RESTORATION_KIND}' AND NOT NEW.is_active THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = '{STATE_TABLE} rejected assertion '
                        || NEW.assertion_id::text
                        || ': a {RESTORATION_KIND} assertion must state is_active = true',
                    HINT = 'Restore by appending a {RESTORATION_KIND} assertion whose '
                        || 'state is active.';
            END IF;

            RETURN NEW;
        END
        $$
    """


def _state_guard_trigger_sql() -> str:
    return f"""
        CREATE TRIGGER {STATE_GUARD_TRIGGER}
        BEFORE INSERT ON {STATE_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {STATE_GUARD_FUNCTION}()
    """


def _append_only_function_sql() -> str:
    """Mirror the append-only guarantee 0018 gives the assertion this payload hangs from."""

    return f"""
        CREATE OR REPLACE FUNCTION {APPEND_ONLY_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            offending_column text;
        BEGIN
            IF NEW.assertion_id IS DISTINCT FROM OLD.assertion_id THEN
                offending_column := 'assertion_id';
            ELSIF NEW.is_active IS DISTINCT FROM OLD.is_active THEN
                offending_column := 'is_active';
            END IF;

            IF offending_column IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = '{STATE_TABLE} is append-only: the state of assertion '
                        || OLD.assertion_id::text
                        || ' cannot be rewritten (column ' || offending_column || ')',
                    HINT = 'Append a new assertion whose supersedes_assertion_id names '
                        || 'this one, and give it its own attachment state.';
            END IF;

            RETURN NEW;
        END
        $$
    """


def _append_only_trigger_sql() -> str:
    return f"""
        CREATE TRIGGER {APPEND_ONLY_TRIGGER}
        BEFORE UPDATE ON {STATE_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}()
    """


def _visible_function_sql() -> str:
    """The bitemporal visibility predicate, defined exactly once.

    An attachment state is visible at ``(act_date, known_at)`` when the effective
    interval ``[effective_from, effective_until)`` contains ``act_date``, the
    assertion was already recorded at ``known_at``, and nothing recorded by
    ``known_at`` supersedes it. A supersession appended *later* than ``known_at``
    is invisible, which is what makes correction non-destructive: the earlier
    known time keeps resolving to the earlier evidence.
    """

    return f"""
        CREATE OR REPLACE FUNCTION {VISIBLE_FUNCTION}(
            attachment_id uuid,
            act_date timestamptz,
            known_at timestamptz
        )
        RETURNS TABLE (
{_RESOLUTION_COLUMNS}
        )
        LANGUAGE sql
        STABLE
        AS $$
            SELECT
                a.assertion_id,
                a.object_id,
                a.revision_no,
                a.assertion_kind::text,
                s.is_active,
                a.effective_from,
                a.effective_until,
                a.recorded_at
            FROM {ASSERTION_TABLE} AS a
            JOIN {STATE_TABLE} AS s ON s.assertion_id = a.assertion_id
            WHERE a.object_id = attachment_id
              AND a.payload_family = '{PAYLOAD_FAMILY}'
              AND a.recorded_at <= known_at
              AND a.effective_from <= act_date
              AND (a.effective_until IS NULL OR act_date < a.effective_until)
              AND NOT EXISTS (
                  SELECT 1
                  FROM {ASSERTION_TABLE} AS successor
                  WHERE successor.supersedes_assertion_id = a.assertion_id
                    AND successor.recorded_at <= known_at
              )
            ORDER BY a.revision_no
        $$
    """


def _as_of_function_sql() -> str:
    """Resolve the one visible attachment state, or refuse an ambiguous pair.

    Both time parameters default to ``now()``, which PostgreSQL evaluates as the
    transaction timestamp: omitting them resolves both to a single request-start
    instant rather than to two drifting clock reads ({1vse}).
    """

    return f"""
        CREATE OR REPLACE FUNCTION {AS_OF_FUNCTION}(
            attachment_id uuid,
            act_date timestamptz DEFAULT now(),
            known_at timestamptz DEFAULT now()
        )
        RETURNS TABLE (
{_RESOLUTION_COLUMNS}
        )
        LANGUAGE plpgsql
        STABLE
        AS $$
        DECLARE
            visible_count integer;
            conflicting text;
        BEGIN
            SELECT
                count(*),
                string_agg(visible.assertion_id::text, ', ' ORDER BY visible.assertion_id)
            INTO visible_count, conflicting
            FROM {VISIBLE_FUNCTION}(attachment_id, act_date, known_at) AS visible;

            IF visible_count > 1 THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'cardinality_violation',
                    MESSAGE = 'ambiguous attachment state for '
                        || attachment_id::text
                        || ' at act_date=' || act_date::text
                        || ', known_at=' || known_at::text
                        || ': simultaneously accepted assertions ' || conflicting,
                    HINT = 'Supersede or close the effective interval of all but one '
                        || 'accepted assertion; resolution never picks a winner.';
            END IF;

            RETURN QUERY
            SELECT visible.*
            FROM {VISIBLE_FUNCTION}(attachment_id, act_date, known_at) AS visible;
        END
        $$
    """


def upgrade() -> None:
    op.create_table(
        STATE_TABLE,
        # The assertion UUID is the whole key: no attachment-state identity, no
        # revision column, no validity interval, no current pointer.
        sa.Column("assertion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("assertion_id", name=conv(f"pk_{STATE_TABLE}")),
        sa.ForeignKeyConstraint(
            ["assertion_id"],
            [f"{ASSERTION_TABLE}.assertion_id"],
            name=conv(f"fk_{STATE_TABLE}_assertion_id_{ASSERTION_TABLE}"),
            ondelete="CASCADE",
        ),
    )

    op.execute(_state_guard_function_sql())
    op.execute(_state_guard_trigger_sql())
    op.execute(_append_only_function_sql())
    op.execute(_append_only_trigger_sql())
    op.execute(_visible_function_sql())
    op.execute(_as_of_function_sql())


def downgrade() -> None:
    op.execute(
        f"DROP FUNCTION IF EXISTS {AS_OF_FUNCTION}(uuid, timestamptz, timestamptz)"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS {VISIBLE_FUNCTION}(uuid, timestamptz, timestamptz)"
    )
    op.execute(f"DROP TRIGGER IF EXISTS {APPEND_ONLY_TRIGGER} ON {STATE_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {APPEND_ONLY_FUNCTION}()")
    op.execute(f"DROP TRIGGER IF EXISTS {STATE_GUARD_TRIGGER} ON {STATE_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {STATE_GUARD_FUNCTION}()")
    op.drop_table(STATE_TABLE)
