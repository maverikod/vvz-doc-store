"""Materialize the SourceSpan diagnostic carrier declared by ``schema.py``.

``processing_runs`` (0019) records the verdict of a round trip but has nowhere to
put the controlled diagnostic {0o1l} demands when that verdict is a non-match:
both hashes, the first differing byte offset *or* the strict-prefix length
boundary, the caller-authorized and redacted bounded context, and the Document,
Chapter and SemanticChunk identifiers. ``source_spans`` is that place — one
independently referenceable bounded diagnostic per refused region ({c6bx},
{lwap}) — and 0019 deliberately left ``processing_runs.mismatch_span_id`` an
unconstrained UUID until this revision existed.

**Why so few constraints.** A refused publication rolls its hierarchy back
({xmpp}), so the span outlives the rows it diagnosed. ``document_id``,
``chapter_id`` and ``chunk_id`` are therefore plain UUID columns with no foreign
key: the span records *what it diagnosed*, not rows that must continue to exist.
``source_state_assertion_id`` is unconstrained and nullable for the same reason —
a refusal accepted no ``SourceState`` for the span to hang from, and a nullable
foreign key would still demand a row the refusal never created. The one real
reference is ``produced_by_run_id``, and it is ``RESTRICT`` so a run cannot be
deleted while a diagnostic still cites it.

``ck_source_spans_exactly_one_difference_locator`` is the shape of {0o1l}'s own
disjunction: a non-match is located by a byte offset, a strict-prefix length
boundary by a length boundary, and never by both or neither. The two presence
checks make a span unrepresentable unless it says under whose authorization and
under which redaction policy its bounded context may be read.

**No indexes, and no server default on** ``redaction_applied``. Every sibling
provenance table of 0019 carries lookup indexes and every other non-null boolean
in the schema carries a default, but the ORM declares neither here, and this
revision renders exactly what the ORM declares — nothing more. Adding either on
one side only would desynchronize
``test_orm_declarations_are_the_migrated_provenance_tables``, which compares the
emitted ``CREATE TABLE`` against ``CreateTable(SourceSpan.__table__)`` verbatim.

**Registry enrolment is deliberately absent**, exactly as for
``primary_sources`` and ``processing_runs``: a diagnostic is evidence *about*
registered objects, not itself a registered addressable object. It has no
``entity_table``/``entity_id`` columns and gets no ``register_entity_uuid``
trigger; ``ck_entity_uuid_registry_kind_allowlisted`` (0018a) would reject such a
kind outright, and the allowlist lives in ``schema.py``, outside this revision.

**``processing_runs`` is not touched.** The deferred foreign key from
``mismatch_span_id`` to ``source_spans.span_id`` is not added here: 0019's
rendered ``CREATE TABLE`` is compared against its ORM declaration verbatim, and
any change to the run table's shape would break that parity.

Revision identifiers in this repository are full file slugs, and migrations are
ordered by ``down_revision`` through
``doc_store_server.db.migrations.apply_migrations``; there is no alembic CLI.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import conv


revision = "0020_source_span_diagnostics"
down_revision = "0019_source_provenance"
branch_labels = None
depends_on = None


SPAN_TABLE = "source_spans"
RUN_TABLE = "processing_runs"


def _create_source_spans() -> None:
    op.create_table(
        SPAN_TABLE,
        sa.Column("span_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("produced_by_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Unconstrained on purpose: a refused publication accepted no SourceState
        # for the span to reference.
        sa.Column(
            "source_state_assertion_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        # Also unconstrained: the rolled-back hierarchy is gone, and the span must
        # still name what it diagnosed.
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("character_start", sa.BigInteger(), nullable=True),
        sa.Column("character_end", sa.BigInteger(), nullable=True),
        sa.Column("byte_offset", sa.BigInteger(), nullable=True),
        sa.Column("length_boundary", sa.BigInteger(), nullable=True),
        sa.Column("context_before", sa.Text(), nullable=True),
        sa.Column("context_after", sa.Text(), nullable=True),
        sa.Column("authorization_decision", sa.String(length=32), nullable=False),
        sa.Column("redaction_policy", sa.String(length=64), nullable=False),
        # No server default: whether the policy actually altered what is stored is
        # a recorded fact about this span, never an assumed one.
        sa.Column("redaction_applied", sa.Boolean(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("bounding_box", postgresql.JSONB(), nullable=True),
        sa.Column("fragment_sha256", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("span_id", name=conv(f"pk_{SPAN_TABLE}")),
        # A span cannot exist without saying under what authority and under which
        # policy its bounded context may be disclosed.
        sa.CheckConstraint(
            "authorization_decision <> ''",
            name=conv(f"ck_{SPAN_TABLE}_authorization_decision_present"),
        ),
        sa.CheckConstraint(
            "redaction_policy <> ''",
            name=conv(f"ck_{SPAN_TABLE}_redaction_policy_present"),
        ),
        # {0o1l}: a first differing byte offset, or a length boundary when one
        # value is a strict prefix of the other -- never both, never neither.
        sa.CheckConstraint(
            "(byte_offset IS NULL) <> (length_boundary IS NULL)",
            name=conv(f"ck_{SPAN_TABLE}_exactly_one_difference_locator"),
        ),
        # RESTRICT: a run may not be deleted while a diagnostic still cites it as
        # the execution that produced it.
        sa.ForeignKeyConstraint(
            ["produced_by_run_id"],
            [f"{RUN_TABLE}.run_id"],
            name=conv(f"fk_{SPAN_TABLE}_produced_by_run_id_{RUN_TABLE}"),
            ondelete="RESTRICT",
        ),
    )


def upgrade() -> None:
    _create_source_spans()


def downgrade() -> None:
    op.drop_table(SPAN_TABLE)
