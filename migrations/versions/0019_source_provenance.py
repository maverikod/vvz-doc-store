"""Materialize immutable source, run and source-state provenance, and bind File to it.

This revision gives the three provenance declarations of ``schema.py`` their
tables and makes the exactly-one File association real in the database:

* ``primary_sources`` is the immutable original ({5434}/{SO01}): the byte
  locator, the *original-byte* SHA-256, the encoding as recorded at capture, the
  media type and the free-form capture provenance. Normalization never touches
  this row; a normalized hash belongs to the ``processing_runs`` row that
  computed it and to the ``source_states`` row it produced ({0o1l}).
* ``processing_runs`` is one immutable auditable execution over exactly one
  recorded comparison scope ({hd4e}/{SO03}). The deterministic configuration
  (``configuration_hash`` plus the immutable normalization profile ID, version
  and complete parameter set), the actor, the status, the timestamps and the
  typed document/chapter/chunk outputs are all columns, and
  ``ck_processing_runs_run_succeeds_only_on_equal_hashes`` makes a ``succeeded``
  status *unrepresentable* unless both hashes exist, are equal and the outcome
  is a match ({0o1l}). A non-match must instead carry its difference evidence.
* ``source_states`` is the ``source_state`` family payload of
  ``temporal_assertions``, keyed by the assertion UUID exactly as
  ``content_attachment_states`` is ({73a1}/{SO02}/{ik6b}). It holds no identity,
  no revision counter, no effective interval, no ``recorded_at`` and no
  current-state pointer: every temporal property already belongs to the
  assertion row, and repeating any of it here would be the second version
  system the one-version rule forbids.

**What happens to a database that already contains files.** ``files`` predates
this revision and ``files.primary_source_id`` is declared NOT NULL, unique and
ON DELETE RESTRICT. A NOT NULL foreign key cannot simply be added to a populated
table, and the column must not be left nullable while the ORM says otherwise, so
this revision stages it in five steps:

1. **Preflight, before any DDL.** Every existing File is inspected for the facts
   a lawful ``PrimarySource`` needs: a non-empty ``path`` (the byte locator), a
   non-empty ``body_sha256`` (the original-byte hash) and a non-negative
   ``byte_length``. If any File lacks them the migration *refuses to proceed*,
   naming every offending File UUID, and the transaction rolls back with no
   schema change at all. Nothing is dropped, defaulted or invented to get past
   an unbackfillable row.
2. The three tables and their guards are created.
3. ``files.primary_source_id`` is added **nullable**.
4. Each existing File is given its own synthesized ``PrimarySource``, in one
   statement. The synthesized row is not a fabrication: its locator, hash, media
   type, byte length and creation instant are the File's own recorded values,
   restated in the immutable-source vocabulary. The single fact a pre-0019 File
   never carried is the capture encoding, which is therefore recorded honestly
   as ``'unknown'`` with ``"recorded_encoding_known": false`` in ``provenance``
   rather than guessed at; ``provenance`` also names this revision, the source
   table, the originating File UUID and the File's ``content_sha256``, so the
   backfilled originals stay distinguishable from captured ones forever.
5. Only then is the column set NOT NULL and given its unique constraint and its
   RESTRICT foreign key. A post-backfill check re-reads the table and aborts
   with a named error if any File is still unbound, so the NOT NULL step can
   never be reached by a partially backfilled table.

An empty ``files`` table therefore takes the same path and does nothing in
steps 1 and 4. No File row is ever deleted or rewritten beyond acquiring its
``primary_source_id``.

Column position: ``primary_source_id`` is declared third in the ORM but arrives
last here, because ``ALTER TABLE ... ADD COLUMN`` appends. That is an ordinal
difference only; type, nullability, uniqueness and referential action match.

**Registry enrolment is deliberately absent.** ``primary_source`` and
``processing_run`` are not in ``REGISTRY_KIND_LOCATORS``, and
``ck_entity_uuid_registry_kind_allowlisted`` (0018a) would reject those kinds
outright, so no ``trg_*_register_entity_uuid`` trigger is installed for either
table. Registry integration here is what the boundary already provides:
``source_states`` hangs from a ``temporal_assertions`` row whose ``object_id``
is a foreign key into ``entity_uuid_registry`` (0018), so a source state always
speaks about a registered object without this revision restating it.
``primary_sources.id`` and ``processing_runs.run_id`` are UUIDv4 identities that
no registered kind claims yet; enrolling them means adding the allowlist entry,
the triggers and the forward-declaration set together, which spans files this
revision may not touch.

Immutability is enforced at the database boundary rather than by convention:
``primary_sources`` refuses every UPDATE, ``processing_runs`` refuses any UPDATE
once its status is terminal and any rewrite of its audit identity before then,
and ``source_states`` mirrors the append-only guarantee 0018 gives the assertion
it hangs from. The ``source_states`` insert guard additionally refuses a payload
whose assertion belongs to another family and one whose ``primary_source_id``
disagrees with the producing run's, which would otherwise record a state of one
original as produced by a run over a different one.

Deliberately out of scope: the SourceSpan diagnostic table
(``processing_runs.mismatch_span_id`` stays an unconstrained UUID until it
exists), any runtime service or command, and the attachment edge table.

Revision identifiers in this repository are full file slugs, and migrations are
ordered by ``down_revision`` through
``doc_store_server.db.migrations.apply_migrations``; there is no alembic CLI.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import conv


revision = "0019_source_provenance"
down_revision = "0018b_content_attachment_temporal_state"
branch_labels = None
depends_on = None


SOURCE_TABLE = "primary_sources"
RUN_TABLE = "processing_runs"
STATE_TABLE = "source_states"
FILE_TABLE = "files"
ASSERTION_TABLE = "temporal_assertions"

FILE_SOURCE_COLUMN = "primary_source_id"

# The payload family on the shared assertion whose payload table is source_states.
PAYLOAD_FAMILY = "source_state"

# Statuses after which a run is history and may not be rewritten at all.
TERMINAL_RUN_STATUSES: tuple[str, ...] = ("succeeded", "failed", "aborted")

# The audit identity of a run: what it consumed, under what configuration, on
# whose behalf and when it was recorded. None of it may drift, even in flight.
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

# Every payload column of a source state; an accepted state is superseded, never
# edited, so a change to any of these is a rewrite of history.
STATE_PAYLOAD_COLUMNS: tuple[str, ...] = (
    "assertion_id",
    "primary_source_id",
    "produced_by_run_id",
    "manifest",
    "reconstruction_locator",
    "checksum_algorithm",
    "restoration_sha256",
    "reconstructed_sha256",
    "recorded_encoding",
    "comparison_scope",
)

SOURCE_IMMUTABLE_FUNCTION = "doc_store_primary_sources_immutable"
SOURCE_IMMUTABLE_TRIGGER = "trg_primary_sources_immutable"
RUN_IMMUTABLE_FUNCTION = "doc_store_processing_runs_immutable"
RUN_IMMUTABLE_TRIGGER = "trg_processing_runs_immutable"
STATE_GUARD_FUNCTION = "doc_store_source_state_guard"
STATE_GUARD_TRIGGER = "trg_source_states_guard"
STATE_APPEND_ONLY_FUNCTION = "doc_store_source_states_append_only"
STATE_APPEND_ONLY_TRIGGER = "trg_source_states_append_only"

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


def _backfill_preflight_sql() -> str:
    """Refuse the whole revision, before any DDL, if a File cannot name an original.

    ``files.primary_source_id`` is required, so a File that carries no lawful
    locator or original hash has no representable answer. Rather than invent one
    or drop the row, this aborts the transaction and names every offending File
    so an operator can repair or remove it and re-run.
    """

    return f"""
        DO $$
        DECLARE
            unbackfillable integer;
            offending text;
        BEGIN
            SELECT
                count(*),
                string_agg(f.id::text, ', ' ORDER BY f.id)
            INTO unbackfillable, offending
            FROM {FILE_TABLE} AS f
            WHERE f.path IS NULL
               OR f.path = ''
               OR f.body_sha256 IS NULL
               OR f.body_sha256 = ''
               OR f.byte_length < 0;

            IF unbackfillable > 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'not_null_violation',
                    MESSAGE = '{revision} refuses to proceed: '
                        || unbackfillable::text
                        || ' file(s) cannot name an immutable PrimarySource: '
                        || offending,
                    HINT = 'Every File requires exactly one original with a non-empty '
                        || 'locator, a non-empty original SHA-256 and a non-negative '
                        || 'byte length. Repair path, body_sha256 and byte_length on the '
                        || 'listed files, or delete them, then re-run this migration. '
                        || 'No schema change has been made.';
            END IF;
        END
        $$
    """


def _backfill_sql() -> str:
    """Give every existing File its own original, restated from the File's own facts.

    One statement, so the inserted originals and the File rows that name them
    cannot diverge: the ``pairs`` CTE is MATERIALIZED, which pins one
    ``gen_random_uuid()`` per File for both the INSERT and the UPDATE.
    """

    return f"""
        WITH pairs AS MATERIALIZED (
            SELECT
                f.id AS file_id,
                gen_random_uuid() AS source_id,
                f.path AS source_locator,
                f.media_type AS media_type,
                f.byte_length AS byte_length,
                f.checksum_algorithm AS checksum_algorithm,
                f.body_sha256 AS original_sha256,
                f.content_sha256 AS content_sha256,
                f.created_at AS created_at
            FROM {FILE_TABLE} AS f
            WHERE f.{FILE_SOURCE_COLUMN} IS NULL
        ),
        inserted AS (
            INSERT INTO {SOURCE_TABLE} (
                id, source_locator, media_type, recorded_encoding, byte_length,
                checksum_algorithm, original_sha256, provenance, created_at
            )
            SELECT
                p.source_id,
                p.source_locator,
                p.media_type,
                -- Never recorded by a pre-0019 File; said so rather than guessed.
                'unknown',
                p.byte_length,
                COALESCE(NULLIF(p.checksum_algorithm, ''), 'sha256'),
                p.original_sha256,
                jsonb_strip_nulls(
                    jsonb_build_object(
                        'captured_by', '{revision}',
                        'derived_from', '{FILE_TABLE}',
                        'file_id', p.file_id::text,
                        'recorded_encoding_known', false,
                        'file_content_sha256', p.content_sha256
                    )
                ),
                p.created_at
            FROM pairs AS p
            RETURNING id
        )
        UPDATE {FILE_TABLE} AS f
        SET {FILE_SOURCE_COLUMN} = p.source_id
        FROM pairs AS p
        WHERE f.id = p.file_id
    """


def _backfill_verification_sql() -> str:
    """Refuse to set NOT NULL over a partially backfilled table."""

    return f"""
        DO $$
        DECLARE
            unbound integer;
        BEGIN
            SELECT count(*) INTO unbound
            FROM {FILE_TABLE}
            WHERE {FILE_SOURCE_COLUMN} IS NULL;

            IF unbound > 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'not_null_violation',
                    MESSAGE = '{revision} backfill left '
                        || unbound::text
                        || ' file(s) without a PrimarySource',
                    HINT = 'The exactly-one source association cannot be enforced over a '
                        || 'partially backfilled table; the transaction is rolled back.';
            END IF;
        END
        $$
    """


def _source_immutable_function_sql() -> str:
    """An original is never edited: a correction is a new original with a new UUID."""

    return f"""
        CREATE OR REPLACE FUNCTION {SOURCE_IMMUTABLE_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = '{SOURCE_TABLE} is immutable: original '
                    || OLD.id::text
                    || ' cannot be rewritten',
                HINT = 'A correction is a new PrimarySource with a new UUID; a change of '
                    || 'state is an appended TemporalAssertion carrying a SourceState '
                    || 'payload. Original-byte preservation is never weakened in place.';
        END
        $$
    """


def _source_immutable_trigger_sql() -> str:
    return f"""
        CREATE TRIGGER {SOURCE_IMMUTABLE_TRIGGER}
        BEFORE UPDATE ON {SOURCE_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {SOURCE_IMMUTABLE_FUNCTION}()
    """


def _run_immutable_function_sql() -> str:
    """A run's audit identity never drifts, and a terminal run is closed history."""

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


def _run_immutable_trigger_sql() -> str:
    return f"""
        CREATE TRIGGER {RUN_IMMUTABLE_TRIGGER}
        BEFORE UPDATE ON {RUN_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {RUN_IMMUTABLE_FUNCTION}()
    """


def _state_guard_function_sql() -> str:
    """Refuse a payload that misreports its family or its source lineage.

    The assertion's ``object_id`` is already a foreign key into
    ``entity_uuid_registry`` (0018), so a source state necessarily speaks about a
    registered object and this guard does not restate it. What the shared
    assertion cannot check is that the state belongs to the family whose table
    this is, and that the original it restores is the original the producing run
    actually consumed.
    """

    return f"""
        CREATE OR REPLACE FUNCTION {STATE_GUARD_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            asserted_family text;
            run_source_id uuid;
        BEGIN
            SELECT a.payload_family
            INTO asserted_family
            FROM {ASSERTION_TABLE} AS a
            WHERE a.assertion_id = NEW.assertion_id;

            IF NOT FOUND THEN
                -- The foreign key is an AFTER-row constraint trigger and has not
                -- fired yet. Let it report the missing assertion itself rather
                -- than masking a referential error with a family error.
                RETURN NEW;
            END IF;

            IF asserted_family <> '{PAYLOAD_FAMILY}' THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = '{STATE_TABLE} rejected assertion '
                        || NEW.assertion_id::text
                        || ': payload family is '
                        || asserted_family
                        || ', not {PAYLOAD_FAMILY}',
                    HINT = 'A family payload table may only key assertions of its own '
                        || 'payload family.';
            END IF;

            SELECT r.primary_source_id
            INTO run_source_id
            FROM {RUN_TABLE} AS r
            WHERE r.run_id = NEW.produced_by_run_id;

            IF FOUND AND run_source_id IS DISTINCT FROM NEW.primary_source_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = '{STATE_TABLE} rejected assertion '
                        || NEW.assertion_id::text
                        || ': state restores original '
                        || NEW.primary_source_id::text
                        || ' but run '
                        || NEW.produced_by_run_id::text
                        || ' consumed '
                        || run_source_id::text,
                    HINT = 'A SourceState is produced by a ProcessingRun over the same '
                        || 'PrimarySource; the evidence chain may not fork.';
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


def _state_append_only_function_sql() -> str:
    """Mirror the append-only guarantee 0018 gives the assertion this payload hangs from."""

    return f"""
        CREATE OR REPLACE FUNCTION {STATE_APPEND_ONLY_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            offending_column text;
        BEGIN
{_distinct_from_ladder(STATE_PAYLOAD_COLUMNS, " " * 12)}

            IF offending_column IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = '{STATE_TABLE} is append-only: the state of assertion '
                        || OLD.assertion_id::text
                        || ' cannot be rewritten (column ' || offending_column || ')',
                    HINT = 'Restoration appends a new assertion whose '
                        || 'supersedes_assertion_id names this one, and gives it its own '
                        || 'source state.';
            END IF;

            RETURN NEW;
        END
        $$
    """


def _state_append_only_trigger_sql() -> str:
    return f"""
        CREATE TRIGGER {STATE_APPEND_ONLY_TRIGGER}
        BEFORE UPDATE ON {STATE_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {STATE_APPEND_ONLY_FUNCTION}()
    """


def _create_primary_sources() -> None:
    op.create_table(
        SOURCE_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_locator", sa.String(length=2048), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=True),
        sa.Column("recorded_encoding", sa.String(length=64), nullable=False),
        sa.Column("byte_length", sa.BigInteger(), nullable=True),
        sa.Column(
            "checksum_algorithm",
            sa.String(length=32),
            nullable=False,
            server_default="sha256",
        ),
        sa.Column("original_sha256", sa.String(length=128), nullable=False),
        # No server default: the ORM defaults `provenance` in Python, and an
        # empty capture provenance must be written deliberately.
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=conv(f"pk_{SOURCE_TABLE}")),
        sa.CheckConstraint(
            "original_sha256 <> ''",
            name=conv(f"ck_{SOURCE_TABLE}_original_hash_present"),
        ),
        sa.CheckConstraint(
            "source_locator <> ''",
            name=conv(f"ck_{SOURCE_TABLE}_source_locator_present"),
        ),
        sa.CheckConstraint(
            "recorded_encoding <> ''",
            name=conv(f"ck_{SOURCE_TABLE}_recorded_encoding_present"),
        ),
        sa.CheckConstraint(
            "byte_length IS NULL OR byte_length >= 0",
            name=conv(f"ck_{SOURCE_TABLE}_byte_length_nonnegative"),
        ),
    )
    # Content-addressed lookup: find every original with these bytes.
    op.create_index(
        f"ix_{SOURCE_TABLE}_original_sha256", SOURCE_TABLE, ["original_sha256"]
    )


def _create_processing_runs() -> None:
    op.create_table(
        RUN_TABLE,
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("primary_source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_kind", sa.String(length=32), nullable=False),
        sa.Column("comparison_scope", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="pending"
        ),
        sa.Column("configuration_hash", sa.String(length=128), nullable=False),
        sa.Column("normalization_profile_id", sa.String(length=128), nullable=False),
        sa.Column("normalization_profile_version", sa.String(length=64), nullable=False),
        sa.Column("normalization_parameters", postgresql.JSONB(), nullable=False),
        sa.Column("source_encoding", sa.String(length=64), nullable=True),
        sa.Column("reconstructed_encoding", sa.String(length=64), nullable=True),
        sa.Column(
            "checksum_algorithm",
            sa.String(length=32),
            nullable=False,
            server_default="sha256",
        ),
        sa.Column("source_sha256", sa.String(length=128), nullable=True),
        sa.Column("reconstruction_sha256", sa.String(length=128), nullable=True),
        sa.Column("integrity_outcome", sa.String(length=32), nullable=True),
        sa.Column("first_difference_offset", sa.BigInteger(), nullable=True),
        # Unconstrained on purpose: the SourceSpan table belongs to a later step
        # of this branch, and a foreign key is added when that table exists.
        sa.Column("mismatch_span_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("diagnostic_authorization", sa.String(length=32), nullable=True),
        sa.Column("redaction_policy", sa.String(length=64), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("processing_trace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("run_id", name=conv(f"pk_{RUN_TABLE}")),
        sa.CheckConstraint(
            "run_kind IN ('document_create', 'document_update', 'file_ingest', "
            "'integrity_recheck')",
            name=conv(f"ck_{RUN_TABLE}_run_kind_allowlisted"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'aborted')",
            name=conv(f"ck_{RUN_TABLE}_status_allowlisted"),
        ),
        sa.CheckConstraint(
            "integrity_outcome IS NULL OR integrity_outcome IN "
            "('match', 'mismatch', 'length_boundary')",
            name=conv(f"ck_{RUN_TABLE}_integrity_outcome_allowlisted"),
        ),
        sa.CheckConstraint("actor <> ''", name=conv(f"ck_{RUN_TABLE}_actor_present")),
        sa.CheckConstraint(
            "comparison_scope <> ''",
            name=conv(f"ck_{RUN_TABLE}_comparison_scope_present"),
        ),
        sa.CheckConstraint(
            "configuration_hash <> ''",
            name=conv(f"ck_{RUN_TABLE}_configuration_hash_present"),
        ),
        sa.CheckConstraint(
            "normalization_profile_id <> '' AND normalization_profile_version <> ''",
            name=conv(f"ck_{RUN_TABLE}_normalization_profile_identified"),
        ),
        # Success is not a claim a caller may make: it is only representable when
        # both hashes exist, are equal and the outcome is a match ({0o1l}).
        sa.CheckConstraint(
            "status <> 'succeeded' OR ("
            "integrity_outcome = 'match'"
            " AND source_sha256 IS NOT NULL"
            " AND reconstruction_sha256 IS NOT NULL"
            " AND source_sha256 = reconstruction_sha256)",
            name=conv(f"ck_{RUN_TABLE}_run_succeeds_only_on_equal_hashes"),
        ),
        # A non-match must carry its controlled diagnostic, not just a verdict.
        sa.CheckConstraint(
            "integrity_outcome IS NULL OR integrity_outcome = 'match' OR ("
            "source_sha256 IS NOT NULL"
            " AND reconstruction_sha256 IS NOT NULL"
            " AND (first_difference_offset IS NOT NULL OR mismatch_span_id IS NOT NULL))",
            name=conv(f"ck_{RUN_TABLE}_non_match_carries_difference_evidence"),
        ),
        sa.CheckConstraint(
            "first_difference_offset IS NULL OR first_difference_offset >= 0",
            name=conv(f"ck_{RUN_TABLE}_first_difference_offset_nonnegative"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)",
            name=conv(f"ck_{RUN_TABLE}_run_interval_ordered"),
        ),
        # RESTRICT: an original may not be deleted while a run cites it as input.
        sa.ForeignKeyConstraint(
            ["primary_source_id"],
            [f"{SOURCE_TABLE}.id"],
            name=conv(f"fk_{RUN_TABLE}_primary_source_id_{SOURCE_TABLE}"),
            ondelete="RESTRICT",
        ),
        # SET NULL: outputs are typed links, and losing an output must not erase
        # the audit record that the run happened.
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=conv(f"fk_{RUN_TABLE}_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["chapter_id"],
            ["chapters.id"],
            name=conv(f"fk_{RUN_TABLE}_chapter_id_chapters"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["semantic_chunks.id"],
            name=conv(f"fk_{RUN_TABLE}_chunk_id_semantic_chunks"),
            ondelete="SET NULL",
        ),
    )
    # Run lineage: every run over one original, and over one document, newest last.
    op.create_index(
        f"ix_{RUN_TABLE}_primary_source", RUN_TABLE, ["primary_source_id", "created_at"]
    )
    op.create_index(f"ix_{RUN_TABLE}_document", RUN_TABLE, ["document_id", "created_at"])
    op.create_index(f"ix_{RUN_TABLE}_status", RUN_TABLE, ["status", "created_at"])
    op.create_index(f"ix_{RUN_TABLE}_configuration_hash", RUN_TABLE, ["configuration_hash"])
    op.create_index(f"ix_{RUN_TABLE}_operation_id", RUN_TABLE, ["operation_id"])


def _create_source_states() -> None:
    op.create_table(
        STATE_TABLE,
        # The assertion UUID is the whole key: no source-state identity, no
        # revision column, no validity interval, no current pointer.
        sa.Column("assertion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("primary_source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("produced_by_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("reconstruction_locator", sa.String(length=2048), nullable=True),
        sa.Column(
            "checksum_algorithm",
            sa.String(length=32),
            nullable=False,
            server_default="sha256",
        ),
        sa.Column("restoration_sha256", sa.String(length=128), nullable=False),
        sa.Column("reconstructed_sha256", sa.String(length=128), nullable=True),
        sa.Column("recorded_encoding", sa.String(length=64), nullable=False),
        sa.Column("comparison_scope", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("assertion_id", name=conv(f"pk_{STATE_TABLE}")),
        sa.CheckConstraint(
            "restoration_sha256 <> ''",
            name=conv(f"ck_{STATE_TABLE}_restoration_hash_present"),
        ),
        sa.CheckConstraint(
            "recorded_encoding <> ''",
            name=conv(f"ck_{STATE_TABLE}_recorded_encoding_present"),
        ),
        sa.CheckConstraint(
            "comparison_scope <> ''",
            name=conv(f"ck_{STATE_TABLE}_comparison_scope_present"),
        ),
        # Lossless restoration needs an actual manifest or an actual locator.
        sa.CheckConstraint(
            "manifest <> '{}'::jsonb OR reconstruction_locator IS NOT NULL",
            name=conv(f"ck_{STATE_TABLE}_lossless_manifest_or_locator"),
        ),
        sa.ForeignKeyConstraint(
            ["assertion_id"],
            [f"{ASSERTION_TABLE}.assertion_id"],
            name=conv(f"fk_{STATE_TABLE}_assertion_id_{ASSERTION_TABLE}"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["primary_source_id"],
            [f"{SOURCE_TABLE}.id"],
            name=conv(f"fk_{STATE_TABLE}_primary_source_id_{SOURCE_TABLE}"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["produced_by_run_id"],
            [f"{RUN_TABLE}.run_id"],
            name=conv(f"fk_{STATE_TABLE}_produced_by_run_id_{RUN_TABLE}"),
            ondelete="RESTRICT",
        ),
    )
    # Source-version lookup: every recorded state of one original, and every
    # state one run produced.
    op.create_index(f"ix_{STATE_TABLE}_primary_source", STATE_TABLE, ["primary_source_id"])
    op.create_index(f"ix_{STATE_TABLE}_produced_by_run", STATE_TABLE, ["produced_by_run_id"])
    op.create_index(
        f"ix_{STATE_TABLE}_restoration_sha256", STATE_TABLE, ["restoration_sha256"]
    )


def _bind_files_to_primary_sources() -> None:
    """Stage the NOT NULL association over an already populated ``files`` table."""

    op.add_column(
        FILE_TABLE,
        sa.Column(FILE_SOURCE_COLUMN, postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(_backfill_sql())
    op.execute(_backfill_verification_sql())
    op.alter_column(FILE_TABLE, FILE_SOURCE_COLUMN, nullable=False)
    # Unique: no two Files claim the same original. RESTRICT: the original
    # cannot be deleted out from under a File.
    op.create_unique_constraint(
        conv(f"uq_{FILE_TABLE}_{FILE_SOURCE_COLUMN}"), FILE_TABLE, [FILE_SOURCE_COLUMN]
    )
    op.create_foreign_key(
        conv(f"fk_{FILE_TABLE}_{FILE_SOURCE_COLUMN}_{SOURCE_TABLE}"),
        FILE_TABLE,
        SOURCE_TABLE,
        [FILE_SOURCE_COLUMN],
        ["id"],
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    # Before any DDL: a File that cannot name an original aborts the revision.
    op.execute(_backfill_preflight_sql())

    _create_primary_sources()
    _create_processing_runs()
    _create_source_states()

    op.execute(_source_immutable_function_sql())
    op.execute(_source_immutable_trigger_sql())
    op.execute(_run_immutable_function_sql())
    op.execute(_run_immutable_trigger_sql())
    op.execute(_state_guard_function_sql())
    op.execute(_state_guard_trigger_sql())
    op.execute(_state_append_only_function_sql())
    op.execute(_state_append_only_trigger_sql())

    _bind_files_to_primary_sources()


def downgrade() -> None:
    # The File association goes first: its RESTRICT foreign key would otherwise
    # refuse to let primary_sources be dropped.
    op.drop_constraint(
        conv(f"fk_{FILE_TABLE}_{FILE_SOURCE_COLUMN}_{SOURCE_TABLE}"),
        FILE_TABLE,
        type_="foreignkey",
    )
    op.drop_constraint(
        conv(f"uq_{FILE_TABLE}_{FILE_SOURCE_COLUMN}"), FILE_TABLE, type_="unique"
    )
    op.drop_column(FILE_TABLE, FILE_SOURCE_COLUMN)

    op.execute(f"DROP TRIGGER IF EXISTS {STATE_APPEND_ONLY_TRIGGER} ON {STATE_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {STATE_APPEND_ONLY_FUNCTION}()")
    op.execute(f"DROP TRIGGER IF EXISTS {STATE_GUARD_TRIGGER} ON {STATE_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {STATE_GUARD_FUNCTION}()")
    op.execute(f"DROP TRIGGER IF EXISTS {RUN_IMMUTABLE_TRIGGER} ON {RUN_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {RUN_IMMUTABLE_FUNCTION}()")
    op.execute(f"DROP TRIGGER IF EXISTS {SOURCE_IMMUTABLE_TRIGGER} ON {SOURCE_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {SOURCE_IMMUTABLE_FUNCTION}()")

    op.drop_index(f"ix_{STATE_TABLE}_restoration_sha256", table_name=STATE_TABLE)
    op.drop_index(f"ix_{STATE_TABLE}_produced_by_run", table_name=STATE_TABLE)
    op.drop_index(f"ix_{STATE_TABLE}_primary_source", table_name=STATE_TABLE)
    op.drop_table(STATE_TABLE)

    op.drop_index(f"ix_{RUN_TABLE}_operation_id", table_name=RUN_TABLE)
    op.drop_index(f"ix_{RUN_TABLE}_configuration_hash", table_name=RUN_TABLE)
    op.drop_index(f"ix_{RUN_TABLE}_status", table_name=RUN_TABLE)
    op.drop_index(f"ix_{RUN_TABLE}_document", table_name=RUN_TABLE)
    op.drop_index(f"ix_{RUN_TABLE}_primary_source", table_name=RUN_TABLE)
    op.drop_table(RUN_TABLE)

    op.drop_index(f"ix_{SOURCE_TABLE}_original_sha256", table_name=SOURCE_TABLE)
    op.drop_table(SOURCE_TABLE)
