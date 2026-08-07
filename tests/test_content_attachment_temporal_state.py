"""Focused regressions: ContentAttachment activity preserves append-only as-of visibility.

ContentAttachment activity is not a mutable flag; it is a claim said in the one
vocabulary the store has for claims, the shared ``TemporalAssertion``
({j7vu}/{ID08}). Creation appends a ``payload`` assertion whose state is active,
deactivation appends a ``deletion`` superseding it, restoration appends a
``restoration`` superseding the deactivation. This module proves four properties
of migration ``0018b_content_attachment_temporal_state`` and the
``ContentAttachmentState`` payload mapping that mirrors it:

* append-only preservation: an accepted state row is superseded, never rewritten
  ({1hst}/{BT06});
* as-of visibility across the three transitions: the half-open effective
  interval ({tbfi}), the ``recorded_at <= known_at`` transaction bound ({405f}),
  and a supersession that is itself invisible before it was recorded, which is
  exactly what keeps an earlier known time resolving to the earlier evidence
  after a deactivation or a restoration is appended ({xmk2}/{424z});
* transition/state agreement plus family and registry binding at the insert
  boundary;
* ambiguity rejection: two simultaneously accepted states raise
  ``cardinality_violation`` instead of a winner ({8ysr}/{BT10}), and both time
  parameters default to one request-start instant ({1vse}/{BT05}).

Everything here is offline and deterministic. The migration is rendered to SQL
with alembic's ``as_sql`` mode exactly as ``tests/test_entity_uuid_registry.py``
does, so no PostgreSQL instance is required. The visibility predicate is plain
SQL rather than plpgsql, so its ``WHERE`` clause is lifted verbatim out of the
rendered DDL and executed against an in-memory SQLite fixture: the three
transitions are therefore *evaluated*, not merely pattern-matched. What genuinely
cannot run without PostgreSQL — plpgsql trigger dispatch and ``RAISE EXCEPTION``
SQLSTATEs — is asserted against the emitted function bodies instead of skipped.
"""

from __future__ import annotations

import importlib.util
import io
import re
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from doc_store_server.db.schema import REGISTRY_KINDS, ContentAttachmentState, metadata


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "versions"
STATE_MIGRATION = "0018b_content_attachment_temporal_state"
ASSERTION_MIGRATION = "0018_bitemporal_assertions"

# The attachment object every scenario below speaks about, and one that no
# assertion mentions, so object scoping is exercised rather than assumed.
ATTACHMENT_ID = "5c7n0000-0000-4000-8000-00000000c125"
OTHER_ATTACHMENT_ID = "5c7n0000-0000-4000-8000-0000000000ff"

# ISO-8601 instants sort lexicographically, which is what lets the rendered
# comparison operators run unchanged against a text-typed SQLite fixture.
CREATED_AT = "2026-01-01T00:00:00+00:00"
DEACTIVATED_AT = "2026-02-01T00:00:00+00:00"
RESTORED_AT = "2026-03-01T00:00:00+00:00"
BEFORE_DEACTIVATION = "2026-01-15T00:00:00+00:00"
BETWEEN_TRANSITIONS = "2026-02-15T00:00:00+00:00"
AFTER_RESTORATION = "2026-03-15T00:00:00+00:00"

CREATION_ASSERTION = "aaaa0001-0000-4000-8000-000000000001"
DEACTIVATION_ASSERTION = "aaaa0002-0000-4000-8000-000000000002"
RESTORATION_ASSERTION = "aaaa0003-0000-4000-8000-000000000003"

_ASSERTION_COLUMNS = (
    "assertion_id",
    "object_id",
    "payload_family",
    "assertion_kind",
    "revision_no",
    "supersedes_assertion_id",
    "effective_from",
    "effective_until",
    "recorded_at",
)

# The three parameters of the rendered predicate, rebound as SQLite placeholders.
_PREDICATE_PARAMETERS = ("attachment_id", "act_date", "known_at")


def _load_migration(name: str) -> ModuleType:
    path = MIGRATIONS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offline_sql(module: ModuleType, function: str = "upgrade") -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        getattr(module, function)()
    return output.getvalue()


def _state_migration() -> ModuleType:
    return _load_migration(STATE_MIGRATION)


def _upgrade_sql() -> str:
    return _offline_sql(_state_migration())


def _squeeze(sql: str) -> str:
    """Collapse the rendered layout so assertions read as SQL, not as whitespace."""

    return " ".join(sql.split())


def _statement(sql: str, opening: str) -> str:
    """Return the one rendered statement beginning with ``opening``.

    Every statement the migration emits starts at column zero, so this also
    proves each function and trigger is created exactly once.
    """

    matches = [
        chunk.strip()
        for chunk in re.split(r"(?m)^(?=CREATE )", sql)
        if chunk.strip().startswith(opening)
    ]
    assert len(matches) == 1, f"expected exactly one {opening!r} statement, got {len(matches)}"
    return matches[0]


def _visibility_where_clause() -> str:
    """Lift the visibility predicate verbatim out of the emitted DDL."""

    migration = _state_migration()
    body = _squeeze(
        _statement(_upgrade_sql(), f"CREATE OR REPLACE FUNCTION {migration.VISIBLE_FUNCTION}(")
    )
    start = body.index("WHERE a.object_id")
    end = body.index("ORDER BY a.revision_no")
    return body[start:end].strip()


def _sqlite_predicate(where_clause: str) -> str:
    predicate = where_clause
    for parameter in _PREDICATE_PARAMETERS:
        predicate = re.sub(rf"(?<![.\w]){parameter}\b", f":{parameter}", predicate)
    return predicate


def _visible(
    assertions: list[dict[str, Any]],
    states: dict[str, bool],
    *,
    act_date: str,
    known_at: str,
    attachment_id: str = ATTACHMENT_ID,
) -> list[tuple[str, bool]]:
    """Run the migration's own visibility predicate over a scenario, offline.

    The ``WHERE`` clause is the rendered one, unedited apart from rebinding its
    three parameters; only the projection is local, because the emitted
    projection casts with PostgreSQL syntax.
    """

    connection = sqlite3.connect(":memory:")
    try:
        columns = ", ".join(f"{name} TEXT" for name in _ASSERTION_COLUMNS)
        connection.execute(f"CREATE TABLE temporal_assertions ({columns})")
        connection.execute(
            "CREATE TABLE content_attachment_states ("
            "assertion_id TEXT PRIMARY KEY, is_active INTEGER NOT NULL)"
        )
        placeholders = ", ".join(f":{name}" for name in _ASSERTION_COLUMNS)
        connection.executemany(
            f"INSERT INTO temporal_assertions VALUES ({placeholders})",
            [{name: assertion.get(name) for name in _ASSERTION_COLUMNS} for assertion in assertions],
        )
        connection.executemany(
            "INSERT INTO content_attachment_states VALUES (:assertion_id, :is_active)",
            [
                {"assertion_id": assertion_id, "is_active": int(is_active)}
                for assertion_id, is_active in states.items()
            ],
        )
        rows = connection.execute(
            "SELECT a.assertion_id, s.is_active"
            " FROM temporal_assertions AS a"
            " JOIN content_attachment_states AS s ON s.assertion_id = a.assertion_id"
            f" {_sqlite_predicate(_visibility_where_clause())}"
            " ORDER BY a.revision_no",
            {"attachment_id": attachment_id, "act_date": act_date, "known_at": known_at},
        ).fetchall()
    finally:
        connection.close()
    return [(assertion_id, bool(is_active)) for assertion_id, is_active in rows]


def _assertion(
    assertion_id: str,
    *,
    kind: str,
    revision_no: int,
    recorded_at: str,
    supersedes: str | None = None,
    effective_from: str = CREATED_AT,
    effective_until: str | None = None,
    object_id: str = ATTACHMENT_ID,
    payload_family: str = "content_attachment",
) -> dict[str, Any]:
    return {
        "assertion_id": assertion_id,
        "object_id": object_id,
        "payload_family": payload_family,
        "assertion_kind": kind,
        "revision_no": str(revision_no),
        "supersedes_assertion_id": supersedes,
        "effective_from": effective_from,
        "effective_until": effective_until,
        "recorded_at": recorded_at,
    }


def _activity_history() -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """Creation, then deactivation superseding it, then restoration superseding that."""

    assertions = [
        _assertion(CREATION_ASSERTION, kind="payload", revision_no=1, recorded_at=CREATED_AT),
        _assertion(
            DEACTIVATION_ASSERTION,
            kind="deletion",
            revision_no=2,
            recorded_at=DEACTIVATED_AT,
            supersedes=CREATION_ASSERTION,
        ),
        _assertion(
            RESTORATION_ASSERTION,
            kind="restoration",
            revision_no=3,
            recorded_at=RESTORED_AT,
            supersedes=DEACTIVATION_ASSERTION,
        ),
    ]
    states = {
        CREATION_ASSERTION: True,
        DEACTIVATION_ASSERTION: False,
        RESTORATION_ASSERTION: True,
    }
    return assertions, states


def test_append_only_trigger_refuses_to_rewrite_an_accepted_attachment_state() -> None:
    """{1hst}/{BT06}: an accepted state is superseded by a new assertion, never edited."""

    migration = _state_migration()
    sql = _upgrade_sql()

    trigger = _squeeze(_statement(sql, f"CREATE TRIGGER {migration.APPEND_ONLY_TRIGGER}"))
    assert trigger == (
        f"CREATE TRIGGER {migration.APPEND_ONLY_TRIGGER}"
        f" BEFORE UPDATE ON {migration.STATE_TABLE}"
        f" FOR EACH ROW EXECUTE FUNCTION {migration.APPEND_ONLY_FUNCTION}();"
    )

    guard = _squeeze(
        _statement(sql, f"CREATE OR REPLACE FUNCTION {migration.APPEND_ONLY_FUNCTION}()")
    )
    # Every column the payload carries is evidence, so every column is guarded.
    payload_columns = [column.name for column in ContentAttachmentState.__table__.c]
    assert payload_columns == ["assertion_id", "is_active"]
    for column in payload_columns:
        assert f"NEW.{column} IS DISTINCT FROM OLD.{column}" in guard
    assert "ERRCODE = 'restrict_violation'" in guard
    assert f"'{migration.STATE_TABLE} is append-only" in guard
    # The refusal names the offending column and points at supersession.
    assert "offending_column := 'assertion_id'" in guard
    assert "offending_column := 'is_active'" in guard
    assert "|| offending_column ||" in guard
    assert "Append a new assertion whose supersedes_assertion_id names" in guard

    # Nothing in the migration itself rewrites an accepted state either.
    assert f"UPDATE {migration.STATE_TABLE}" not in sql
    assert f"DELETE FROM {migration.STATE_TABLE}" not in sql


def test_visibility_predicate_keeps_creation_visible_after_deactivation_and_restoration() -> None:
    """{tbfi}/{405f}/{xmk2}: each known time resolves to the evidence accepted by then."""

    assertions, states = _activity_history()
    act_date = "2026-06-01T00:00:00+00:00"

    # Before the deactivation was recorded, the creation is still the answer: the
    # deactivation exists as a row but is invisible at that known time, so its
    # supersession of the creation is invisible too.
    assert _visible(assertions, states, act_date=act_date, known_at=BEFORE_DEACTIVATION) == [
        (CREATION_ASSERTION, True)
    ]
    # Between the two transitions the deactivation is the one accepted state, and
    # the creation is excluded only because a successor naming it was recorded by
    # then -- it was not rewritten or removed.
    assert _visible(assertions, states, act_date=act_date, known_at=BETWEEN_TRANSITIONS) == [
        (DEACTIVATION_ASSERTION, False)
    ]
    # After the restoration, the attachment is active again and the deactivation
    # survives as evidence at its own known time, which the assertion above shows.
    assert _visible(assertions, states, act_date=act_date, known_at=AFTER_RESTORATION) == [
        (RESTORATION_ASSERTION, True)
    ]
    # The very instants of record are already known: `recorded_at <= known_at`.
    assert _visible(assertions, states, act_date=act_date, known_at=DEACTIVATED_AT) == [
        (DEACTIVATION_ASSERTION, False)
    ]
    assert _visible(assertions, states, act_date=act_date, known_at=RESTORED_AT) == [
        (RESTORATION_ASSERTION, True)
    ]

    # The effective interval is half-open: the lower bound is inclusive, the
    # upper bound exclusive, and a NULL upper bound is open ({tbfi}).
    assert _visible(assertions, states, act_date=CREATED_AT, known_at=BEFORE_DEACTIVATION) == [
        (CREATION_ASSERTION, True)
    ]
    assert _visible(
        assertions, states, act_date="2025-12-31T23:59:59+00:00", known_at=AFTER_RESTORATION
    ) == []
    closed = [
        _assertion(
            CREATION_ASSERTION,
            kind="payload",
            revision_no=1,
            recorded_at=CREATED_AT,
            effective_until=DEACTIVATED_AT,
        )
    ]
    closed_states = {CREATION_ASSERTION: True}
    assert _visible(
        closed, closed_states, act_date=BEFORE_DEACTIVATION, known_at=AFTER_RESTORATION
    ) == [(CREATION_ASSERTION, True)]
    assert _visible(
        closed, closed_states, act_date=DEACTIVATED_AT, known_at=AFTER_RESTORATION
    ) == []

    # Resolution is scoped to the object the assertion names ({ss96}) and to this
    # payload family, so an attachment with no assertions resolves to nothing.
    assert _visible(
        assertions,
        states,
        act_date=act_date,
        known_at=AFTER_RESTORATION,
        attachment_id=OTHER_ATTACHMENT_ID,
    ) == []

    # And the emitted clause is the bitemporal one, not an accident of the fixture.
    where = _visibility_where_clause()
    assert "a.recorded_at <= known_at" in where
    assert "a.effective_from <= act_date" in where
    assert "(a.effective_until IS NULL OR act_date < a.effective_until)" in where
    assert f"a.payload_family = '{_state_migration().PAYLOAD_FAMILY}'" in where
    successor_clause = where[where.index("NOT EXISTS ("):]
    assert "successor.supersedes_assertion_id = a.assertion_id" in successor_clause
    assert "successor.recorded_at <= known_at" in successor_clause


def test_guard_binds_deactivation_and_restoration_to_the_state_they_assert() -> None:
    """A deletion may not claim active, a restoration may not claim inactive."""

    migration = _state_migration()
    sql = _upgrade_sql()

    trigger = _squeeze(_statement(sql, f"CREATE TRIGGER {migration.STATE_GUARD_TRIGGER}"))
    assert trigger == (
        f"CREATE TRIGGER {migration.STATE_GUARD_TRIGGER}"
        f" BEFORE INSERT ON {migration.STATE_TABLE}"
        f" FOR EACH ROW EXECUTE FUNCTION {migration.STATE_GUARD_FUNCTION}();"
    )

    guard = _squeeze(
        _statement(sql, f"CREATE OR REPLACE FUNCTION {migration.STATE_GUARD_FUNCTION}()")
    )
    assert f"IF asserted.assertion_kind = '{migration.DEACTIVATION_KIND}' AND NEW.is_active THEN" in guard
    assert f"IF asserted.assertion_kind = '{migration.RESTORATION_KIND}' AND NOT NEW.is_active THEN" in guard
    assert f"a {migration.DEACTIVATION_KIND} assertion must state is_active = false" in guard
    assert f"a {migration.RESTORATION_KIND} assertion must state is_active = true" in guard

    # The payload may only key an assertion of its own family, about an object the
    # ObjectRegistry registered with the matching kind (G-001).
    assert f"IF asserted.payload_family <> '{migration.PAYLOAD_FAMILY}' THEN" in guard
    assert f"FROM {migration.REGISTRY_TABLE} AS r WHERE r.entity_id = asserted.object_id" in guard
    assert f"IF registered_kind IS DISTINCT FROM '{migration.REGISTRY_KIND}' THEN" in guard
    assert guard.count("ERRCODE = 'check_violation'") == 4

    # The transitions reuse the shared assertion vocabulary; they invent none.
    assertion_kinds = _load_migration(ASSERTION_MIGRATION).ASSERTION_KINDS
    assert migration.DEACTIVATION_KIND in assertion_kinds
    assert migration.RESTORATION_KIND in assertion_kinds
    assert migration.REGISTRY_KIND in REGISTRY_KINDS


def test_as_of_resolver_rejects_simultaneously_accepted_states_and_defaults_to_one_instant() -> None:
    """{8ysr}/{BT10}: ambiguity is refused by name; {1vse}: both times default to now()."""

    migration = _state_migration()
    resolver = _squeeze(
        _statement(_upgrade_sql(), f"CREATE OR REPLACE FUNCTION {migration.AS_OF_FUNCTION}(")
    )

    # Both parameters default to now(), the transaction timestamp, so an omitted
    # pair resolves to one request-start instant rather than two clock reads.
    assert "act_date timestamptz DEFAULT now()" in resolver
    assert "known_at timestamptz DEFAULT now()" in resolver

    # The ambiguity check and the returned row are the same predicate, once.
    assert resolver.count(f"FROM {migration.VISIBLE_FUNCTION}(attachment_id, act_date, known_at)") == 2
    assert "IF visible_count > 1 THEN" in resolver
    assert "ERRCODE = 'cardinality_violation'" in resolver
    assert "simultaneously accepted assertions ' || conflicting" in resolver
    assert "string_agg(visible.assertion_id::text" in resolver
    assert "resolution never picks a winner" in resolver
    # Nothing arbitrates: no ordering-based tiebreak stands in for the refusal.
    assert "LIMIT 1" not in resolver
    assert "DISTINCT ON" not in resolver

    # Two accepted assertions over one effective instant really are both visible,
    # which is the condition the refusal above is guarding.
    rival = "aaaa0004-0000-4000-8000-000000000004"
    assertions = [
        _assertion(CREATION_ASSERTION, kind="payload", revision_no=1, recorded_at=CREATED_AT),
        _assertion(rival, kind="payload", revision_no=2, recorded_at=CREATED_AT),
    ]
    states = {CREATION_ASSERTION: True, rival: False}
    visible = _visible(
        assertions, states, act_date=BETWEEN_TRANSITIONS, known_at=AFTER_RESTORATION
    )
    assert [assertion_id for assertion_id, _ in visible] == [CREATION_ASSERTION, rival]

    # Superseding one of them is what resolves the ambiguity -- appending, not editing.
    assertions[1]["supersedes_assertion_id"] = CREATION_ASSERTION
    assert _visible(
        assertions, states, act_date=BETWEEN_TRANSITIONS, known_at=AFTER_RESTORATION
    ) == [(rival, False)]


def test_orm_state_mirrors_the_migrated_content_attachment_states_table() -> None:
    """The mapping is the migrated table: same columns, same constraint names."""

    migration = _state_migration()
    table = ContentAttachmentState.__table__

    assert table.name == migration.STATE_TABLE
    assert metadata.tables[migration.STATE_TABLE] is table

    emitted = _squeeze(_statement(_upgrade_sql(), f"CREATE TABLE {migration.STATE_TABLE}"))
    mapped = _squeeze(str(CreateTable(table).compile(dialect=postgresql.dialect())))
    assert emitted == f"{mapped};"

    # Spelled out, so a silent rename on either side fails here rather than at runtime.
    assert [column.name for column in table.c] == ["assertion_id", "is_active"]
    assert all(column.nullable is False for column in table.c)
    assert table.primary_key.name == f"pk_{migration.STATE_TABLE}"
    assert [column.name for column in table.primary_key.columns] == ["assertion_id"]
    (foreign_key,) = table.foreign_key_constraints
    assert foreign_key.name == (
        f"fk_{migration.STATE_TABLE}_assertion_id_{migration.ASSERTION_TABLE}"
    )
    assert [element.target_fullname for element in foreign_key.elements] == [
        f"{migration.ASSERTION_TABLE}.assertion_id"
    ]
    assert foreign_key.ondelete == "CASCADE"

    # No second version system: the payload owns no identity, interval, revision
    # order or provenance of its own ({j7vu}/{ID08}).
    forbidden = {
        "id",
        "revision_no",
        "effective_from",
        "effective_until",
        "recorded_at",
        "is_current",
        "supersedes_assertion_id",
    }
    assert forbidden.isdisjoint(set(table.c.keys()))
