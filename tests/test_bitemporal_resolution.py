"""Branch regression: the shared bitemporal contract, written and read end to end.

G-002 owns one version identity — ``TemporalAssertion`` — and the semantics that
hang off it. The unit-level modules elsewhere prove each piece in isolation; this
module proves the pieces are the *same* contract: rows appended by
``TemporalAssertionService`` are the rows ``retrieval_commands`` resolves, and the
pair that resolution settles on is the pair a ``TemporalCursor`` pins for replay.

What is proved here, by execution rather than by inspection:

* identity ({ss96}/{ID09}, {j7vu}/{ID08}): every appended version is a fresh
  assertion UUID naming its RegisteredObject through the ObjectRegistry, and the
  assertion table carries no current-state pointer at all;
* append-only history ({1hst}/{BT06}, {4jxy}/{BT03}): the service issues nothing
  but SELECT and INSERT against ``temporal_assertions``, earlier rows survive
  every later mutation byte-for-byte, and ``recorded_at`` is never rewritten;
* half-open effective time ({tbfi}/{BT01}) and immutable transaction time
  ({405f}/{BT02}): the segments of a corrected timeline tile the original
  interval without overlap, and the boundary instant belongs to the later
  segment;
* ambiguity rejection ({8ysr}/{BT10}): the writer refuses an overlapping accepted
  interval and the reader refuses to rank one that reached the table anyway;
* request-start resolution ({1vse}/{BT05}, {eh6d}/{BT04}, {nmu3}/{LC02},
  {nwfn}): both times default to one instant, and list resolution yields at most
  one visible version per object before any ordering or ranking;
* deletion and restoration ({xmk2}/{BT07}, {424z}/{DL02}, {tox8}/{DL01}): an
  exact UUID list with optional registered-descendant closure, appended as
  deletion assertions that leave earlier as-of visibility — and the mutable
  entity soft-delete fields — untouched;
* partial replacement ({ctg0}/{BT11}): one global supersession plus only the
  non-empty left/corrected/right segments, sharing one correction instant;
* stable cursors ({q52o}/{LC03}): the pinned pair, order key, position and
  canonical query identity replay to the identical page, and a mismatch is
  rejected.

The whole module is offline. PostgreSQL statements the service emits are
translated to their SQLite equivalents (``CAST(? AS uuid)``, ``= ANY(...)``,
``FOR UPDATE`` and the ``now()`` transaction clock) and executed against a real
in-memory-style database whose ``temporal_assertions`` and ``entity_uuid_registry``
tables are generated from the ORM declarations themselves, so the declared CHECK,
UNIQUE and FOREIGN KEY predicates genuinely fire. What cannot run without
PostgreSQL — plpgsql trigger dispatch and its ``restrict_violation`` SQLSTATE — is
asserted against the DDL migration 0018 renders, exactly as
``tests/test_content_attachment_temporal_state.py`` does, never skipped.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from chunk_metadata_adapter import ChunkQuery
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import IntegrityError

from doc_store_client.models import (
    TemporalCursor,
    TemporalRequest,
    canonical_query_id,
    with_temporal_bounds,
)
from doc_store_server.commands.retrieval_commands import (
    AmbiguousAssertionError,
    DocumentGetCommand,
    ResolvedTimePair,
    is_assertion_like,
    resolve_time_pair,
    resolve_visible_versions,
    select_visible_assertion,
)
from doc_store_server.db.schema import (
    ASSERTION_KINDS,
    EntityUuidRegistry,
    SemanticChunkVersion,
    TemporalAssertion,
)
from doc_store_server.runtime.temporal_assertions import (
    AMBIGUOUS_EFFECTIVE_INTERVAL,
    OBJECT_NOT_REGISTERED,
    TemporalAssertionError,
    TemporalAssertionService,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "versions"
ASSERTION_MIGRATION = "0018_bitemporal_assertions"

# Effective time: what the world looked like. Transaction time: when we learned
# it. The two axes are deliberately spelled from different years so a mistaken
# comparison across axes cannot accidentally pass.
JANUARY = "2026-01-01T00:00:00+00:00"
FEBRUARY = "2026-02-01T00:00:00+00:00"
MARCH = "2026-03-01T00:00:00+00:00"
APRIL = "2026-04-01T00:00:00+00:00"
MAY = "2026-05-01T00:00:00+00:00"
JULY = "2026-07-01T00:00:00+00:00"
JUST_BEFORE_MARCH = "2026-02-28T23:59:59+00:00"

FIRST_RECORDED_AT = datetime(2027, 1, 1, tzinfo=UTC)

# Minimal typed tables the registered-descendant closure walks. Only the columns
# DESCENDANT_EDGES actually reads are present, plus the mutable entity lifecycle
# fields that temporal deletion must leave alone.
_TYPED_TABLES: dict[str, tuple[str, ...]] = {
    "projects": ("id", "owner_id"),
    "files": ("id", "owner_id"),
    "documents": ("id", "owner_id", "block_meta", "is_deleted", "deleted_at"),
    "chapters": ("id", "document_id"),
    "paragraphs": ("id", "document_id", "chapter_id"),
    "semantic_chunks": ("id", "document_id", "chapter_id", "paragraph_id"),
}

# PostgreSQL constructs the service emits, and their SQLite equivalents. The
# statements themselves are the production ones; only these spellings differ.
_UUID_OR_INSTANT_CAST = re.compile(r"CAST\(\s*\?\s+AS\s+(?:uuid|timestamptz)\s*\)", re.IGNORECASE)
_ARRAY_MEMBERSHIP = re.compile(r"=\s*ANY\(\s*CAST\(\s*\?\s+AS\s+uuid\[\]\s*\)\s*\)", re.IGNORECASE)
_TRANSACTION_CLOCK = re.compile(r"\bnow\(\)")
_ROW_LOCK = " FOR UPDATE"


def _register_sqlite_types() -> None:
    """Make the driver hand back UUIDs and aware instants, as psycopg does."""

    sqlite3.register_adapter(UUID, str)
    sqlite3.register_adapter(datetime, lambda value: value.isoformat())
    sqlite3.register_converter("UUIDTEXT", lambda raw: UUID(raw.decode()))
    sqlite3.register_converter("TIMESTAMPTZ", lambda raw: datetime.fromisoformat(raw.decode()))


def _sqlite_column_type(column: Any) -> str:
    if isinstance(column.type, PGUUID):
        return "UUIDTEXT"
    if isinstance(column.type, DateTime):
        return "TIMESTAMPTZ"
    if isinstance(column.type, Boolean | Integer):
        return "INTEGER"
    return "TEXT"


def _sqlite_ddl(table: Table, *, checks: bool = True) -> str:
    """Render one declared table so its own constraints are the ones enforced."""

    parts = [
        f"{column.name} {_sqlite_column_type(column)}" + ("" if column.nullable else " NOT NULL")
        for column in table.c
    ]
    for constraint in sorted(
        table.constraints, key=lambda item: (type(item).__name__, str(item.name or ""))
    ):
        columns = ", ".join(column.name for column in constraint.columns)
        if isinstance(constraint, PrimaryKeyConstraint):
            parts.append(f"PRIMARY KEY ({columns})")
        elif isinstance(constraint, UniqueConstraint):
            parts.append(f"CONSTRAINT {constraint.name} UNIQUE ({columns})")
        elif isinstance(constraint, CheckConstraint) and checks:
            parts.append(f"CONSTRAINT {constraint.name} CHECK ({constraint.sqltext})")
        elif isinstance(constraint, ForeignKeyConstraint):
            element = list(constraint.elements)[0]
            parts.append(
                f"FOREIGN KEY ({columns}) "
                f"REFERENCES {element.column.table.name}({element.column.name}) "
                f"ON DELETE {constraint.ondelete}"
            )
    return f"CREATE TABLE {table.name} ({', '.join(parts)})"


def _to_sqlite(statement: str, parameters: Any, clock: datetime) -> tuple[str, tuple[Any, ...]]:
    """Translate one emitted PostgreSQL statement, preserving parameter order."""

    params = list(parameters)
    statement = statement.replace(_ROW_LOCK, "")
    statement = _UUID_OR_INSTANT_CAST.sub("?", statement)
    while (match := _ARRAY_MEMBERSHIP.search(statement)) is not None:
        index = statement.count("?", 0, match.start())
        params[index] = json.dumps([str(item) for item in params[index]])
        statement = (
            statement[: match.start()]
            + "IN (SELECT value FROM json_each(?))"
            + statement[match.end() :]
        )
    # The transaction clock is the database's, never the caller's; here it is a
    # database clock the test can step, which is what makes known time legible.
    return _TRANSACTION_CLOCK.sub(f"'{clock.isoformat()}'", statement), tuple(params)


class _OfflineService(TemporalAssertionService):
    """The production service, bound to the offline engine instead of PostgreSQL."""

    def __init__(self, engine: Any) -> None:
        super().__init__("offline://temporal-assertions")
        self._offline_engine = engine

    def _engine(self) -> Any:
        return self._offline_engine


class _Store:
    """An executable stand-in for the assertion database plus its registry."""

    def __init__(self, path: Path) -> None:
        _register_sqlite_types()
        self.clock = FIRST_RECORDED_AT
        self.statements: list[str] = []
        self.engine = create_engine(
            f"sqlite+pysqlite:///{path}",
            connect_args={"detect_types": sqlite3.PARSE_DECLTYPES},
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        event.listen(self.engine, "before_cursor_execute", self._translate, retval=True)
        self._create_schema()
        self.service = _OfflineService(self.engine)
        self.statements.clear()

    @staticmethod
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    def _translate(
        self,
        _connection: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> tuple[str, Any]:
        self.statements.append(statement)
        return _to_sqlite(statement, parameters, self.clock)

    def _create_schema(self) -> None:
        with self.engine.begin() as connection:
            # The registry is the boundary this branch links to, not the contract
            # it owns; its kind/locator allowlist belongs to G-001 and uses a row
            # value SQLite will not accept inside a CHECK, so only its identity
            # columns and primary key are materialized here.
            connection.exec_driver_sql(
                _sqlite_ddl(EntityUuidRegistry.__table__, checks=False)
            )
            connection.exec_driver_sql(_sqlite_ddl(TemporalAssertion.__table__))
            for table, columns in _TYPED_TABLES.items():
                declared = ", ".join(f"{column} TEXT" for column in columns)
                connection.exec_driver_sql(f"CREATE TABLE {table} ({declared})")
            connection.exec_driver_sql(
                "CREATE TABLE document_payloads (assertion_id TEXT PRIMARY KEY, body TEXT)"
            )

    # ------------------------------------------------------------------ setup

    def advance(self, **delta: int) -> datetime:
        """Move the database transaction clock forward between operations."""

        self.clock += timedelta(**delta)
        return self.clock

    def register(self, kind: str, locator: str) -> UUID:
        entity_id = uuid4()
        self.execute(
            "INSERT INTO entity_uuid_registry (entity_id, kind, entity_table, created_at) "
            "VALUES (:entity_id, :kind, :entity_table, :created_at)",
            entity_id=entity_id,
            kind=kind,
            entity_table=locator,
            created_at=self.clock,
        )
        return entity_id

    def insert(self, table: str, **values: Any) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        self.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", **values)

    def execute(self, statement: str, **params: Any) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(statement), params)

    def query(self, statement: str, **params: Any) -> list[dict[str, Any]]:
        with self.engine.begin() as connection:
            rows = connection.execute(text(statement), params).mappings().all()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ reads

    def history(self, object_id: UUID) -> list[dict[str, Any]]:
        return list(self.service.history(object_id=object_id)["items"])

    def assertion_statements(self) -> list[str]:
        return [item for item in self.statements if "temporal_assertions" in item]


@pytest.fixture()
def store(tmp_path: Path) -> _Store:
    return _Store(tmp_path / "bitemporal.sqlite3")


def _copy_document_payload(connection: Any, source: UUID, target: UUID) -> None:
    connection.execute(
        text(
            "INSERT INTO document_payloads (assertion_id, body) "
            "SELECT :target, body FROM document_payloads WHERE assertion_id = :source"
        ),
        {"target": str(target), "source": str(source)},
    )


def _write_document_payload(body: str) -> Any:
    def writer(connection: Any, assertion_id: UUID) -> None:
        connection.execute(
            text("INSERT INTO document_payloads (assertion_id, body) VALUES (:id, :body)"),
            {"id": str(assertion_id), "body": body},
        )

    return writer


def _document(store: _Store) -> UUID:
    object_id = store.register("document", "documents")
    store.insert("documents", id=str(object_id))
    return object_id


def _created(store: _Store, object_id: UUID, *, body: str = "original") -> dict[str, Any]:
    """Append the first open-ended payload assertion, with its family payload."""

    created = store.service.create_assertion(
        object_id=object_id,
        effective_from=JANUARY,
        payload_family="document",
        accepted_by="ingest",
        accepted_via="mcp",
    )
    assertion = created["assertion"]
    store.insert("document_payloads", assertion_id=assertion["assertion_id"], body=body)
    return assertion


def _pair(act_date: str, known_at: datetime) -> ResolvedTimePair:
    return ResolvedTimePair(
        act_date=datetime.fromisoformat(act_date),
        known_at=known_at,
    )


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


def _page(
    rows: list[dict[str, Any]],
    pair: ResolvedTimePair,
    *,
    order_key: str,
    position: int,
    size: int,
) -> list[str]:
    """Resolve first, then order, then cut the page -- in that order, always."""

    resolved = resolve_visible_versions(rows, pair)
    ordered = sorted(
        resolved.values(),
        key=lambda row: tuple(str(row[part]) for part in order_key.split(",")),
    )
    return [str(row["object_id"]) for row in ordered[position : position + size]]


def test_each_appended_version_is_a_new_uuid_naming_its_registered_object(store: _Store) -> None:
    """{ss96}/{ID09}, {j7vu}/{ID08}: identity is the assertion, resolved through the registry."""

    object_id = _document(store)
    created = _created(store, object_id)
    store.advance(days=1)
    closed = store.service.close_interval(assertion_id=created["assertion_id"], effective_until=MAY)
    store.advance(days=1)
    store.service.replace_interval(
        assertion_id=closed["assertion"]["assertion_id"],
        effective_from=MARCH,
        effective_until=APRIL,
    )

    rows = store.history(object_id)
    identities = [row["assertion_id"] for row in rows]
    assert len(identities) == len(set(identities)) == 5
    assert all(UUID(identity).version == 4 for identity in identities)
    # Revision order is monotonic per registered object and derives from accepted
    # append order, not from any mutable current-state pointer.
    assert [row["revision_no"] for row in rows] == [1, 2, 3, 4, 5]

    # Every version names one RegisteredObject directly, and that UUID alone
    # resolves the object in the one registry -- no typed table is consulted.
    assert {row["object_id"] for row in rows} == {str(object_id)}
    registered = store.query(
        "SELECT r.kind, r.entity_table FROM temporal_assertions AS a "
        "JOIN entity_uuid_registry AS r ON r.entity_id = a.object_id "
        "GROUP BY r.kind, r.entity_table"
    )
    assert registered == [{"kind": "document", "entity_table": "documents"}]

    # The declaration points at the registry boundary, and nowhere else.
    foreign_keys = {
        column.name: f"{key.column.table.name}.{key.column.name}"
        for column in TemporalAssertion.__table__.c
        for key in column.foreign_keys
    }
    assert foreign_keys == {
        "object_id": "entity_uuid_registry.entity_id",
        "supersedes_assertion_id": "temporal_assertions.assertion_id",
    }
    # No current pointer, no lifecycle flag: temporal authority is the history.
    assert not {"is_current", "current_version_id", "is_deleted", "deleted_at"} & set(
        TemporalAssertion.__table__.c.keys()
    )


def test_an_assertion_cannot_name_an_unregistered_object(store: _Store) -> None:
    """{ss96}/{ID09}: the RegisteredObject link is enforced, not merely intended."""

    with pytest.raises(TemporalAssertionError) as refusal:
        store.service.create_assertion(
            object_id=uuid4(),
            effective_from=JANUARY,
            payload_family="document",
        )
    assert refusal.value.code == OBJECT_NOT_REGISTERED

    # And the foreign key would refuse the row even if the service did not.
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        store.insert(
            "temporal_assertions",
            assertion_id=uuid4(),
            object_id=uuid4(),
            payload_family="document",
            assertion_kind="payload",
            revision_no=1,
            effective_from=datetime.fromisoformat(JANUARY),
            recorded_at=store.clock,
        )


def test_the_history_only_grows_and_never_rewrites_accepted_evidence(store: _Store) -> None:
    """{1hst}/{BT06}, {4jxy}/{BT03}: every mutation is an append; earlier rows are frozen."""

    object_id = _document(store)
    created = _created(store, object_id)
    snapshots = [store.history(object_id)]

    store.advance(days=1)
    closed = store.service.close_interval(assertion_id=created["assertion_id"], effective_until=MAY)
    assert closed["superseded_assertion_id"] == created["assertion_id"]
    snapshots.append(store.history(object_id))
    store.advance(days=1)
    deleted = store.service.soft_delete(object_ids=[object_id])
    snapshots.append(store.history(object_id))
    store.advance(days=1)
    restored = store.service.restore(
        deletion_assertion_id=deleted["targets"][0]["deletion"]["assertion_id"]
    )
    snapshots.append(store.history(object_id))
    store.advance(days=1)
    replaced = store.service.replace_interval(
        assertion_id=restored["assertion"]["assertion_id"],
        effective_from=MARCH,
        effective_until=APRIL,
    )
    snapshots.append(store.history(object_id))

    # Each history is an exact prefix of the next: rows are added, never edited
    # and never removed, so no correction can rewrite what was accepted before it.
    for earlier, later in zip(snapshots, snapshots[1:]):
        assert len(later) > len(earlier)
        assert later[: len(earlier)] == earlier
    assert replaced["superseded_assertion_id"] == restored["assertion"]["assertion_id"]

    # The transaction time of the first assertion is the one it was accepted with,
    # five mutations later.
    assert snapshots[-1][0]["recorded_at"] == FIRST_RECORDED_AT.isoformat()

    # The service never even asks the database to rewrite a row.
    verbs = {statement.split(None, 1)[0].upper() for statement in store.assertion_statements()}
    assert verbs == {"SELECT", "INSERT"}


def test_the_database_refuses_to_rewrite_accepted_evidence() -> None:
    """{405f}/{BT02}: append-only is a trigger, not a convention of the writer."""

    migration = _load_migration(ASSERTION_MIGRATION)
    sql = _offline_sql(migration)

    assert (
        f"CREATE TRIGGER {migration.APPEND_ONLY_TRIGGER}\n"
        "        BEFORE UPDATE ON temporal_assertions\n"
        "        FOR EACH ROW EXECUTE FUNCTION "
        f"{migration.APPEND_ONLY_FUNCTION}()"
    ) in sql

    # Every column of the declaration except the supersession link is evidence,
    # and every one of them is guarded -- recorded_at among them.
    guarded = set(migration.IMMUTABLE_COLUMNS)
    assert guarded == set(TemporalAssertion.__table__.c.keys()) - {"supersedes_assertion_id"}
    assert len(migration.IMMUTABLE_COLUMNS) == 12
    for column in migration.IMMUTABLE_COLUMNS:
        assert f"IF NEW.{column} IS DISTINCT FROM OLD.{column} THEN" in sql
    # restrict_violation is SQLSTATE 23001, the refusal a rewrite attempt sees.
    assert "ERRCODE = 'restrict_violation'" in sql
    assert "Append a new assertion whose supersedes_assertion_id names" in sql

    # The one permitted update is the supersession link clearing itself, which is
    # exactly what its own ON DELETE SET NULL rule performs.
    assert (
        "AND NEW.supersedes_assertion_id IS DISTINCT FROM OLD.supersedes_assertion_id\n"
        "                AND NEW.supersedes_assertion_id IS NOT NULL THEN"
    ) in sql
    assert "UPDATE temporal_assertions" not in sql
    assert "DELETE FROM temporal_assertions" not in sql


def test_declared_assertion_constraints_reject_malformed_evidence(store: _Store) -> None:
    """{tbfi}/{BT01}, {8nd5}/{BT09}: the shape of an assertion is enforced by the schema."""

    object_id = _document(store)
    _created(store, object_id)

    def append(**overrides: Any) -> None:
        values: dict[str, Any] = {
            "assertion_id": uuid4(),
            "object_id": object_id,
            "payload_family": "document",
            "assertion_kind": "payload",
            "revision_no": 2,
            "effective_from": datetime.fromisoformat(JANUARY),
            "effective_until": None,
            "recorded_at": store.clock,
        }
        values.update(overrides)
        store.insert("temporal_assertions", **values)

    with pytest.raises(IntegrityError, match="revision_positive"):
        append(revision_no=0)
    with pytest.raises(IntegrityError, match="effective_interval_half_open"):
        append(
            effective_from=datetime.fromisoformat(MARCH),
            effective_until=datetime.fromisoformat(MARCH),
        )
    with pytest.raises(IntegrityError, match="assertion_kind_allowlisted"):
        append(assertion_kind="rewrite")
    with pytest.raises(IntegrityError, match="payload_family_present"):
        append(payload_family="")
    # Revision order is unique per registered object, so two rows cannot claim
    # the same place in the accepted order.
    with pytest.raises(
        IntegrityError,
        match=r"UNIQUE constraint failed: temporal_assertions\.object_id, "
        r"temporal_assertions\.revision_no",
    ):
        append(revision_no=1)

    identity = uuid4()
    with pytest.raises(IntegrityError, match="supersedes_another_assertion"):
        append(assertion_id=identity, supersedes_assertion_id=identity)

    # The allowlist the check enforces is the declared vocabulary of evidence.
    assert set(ASSERTION_KINDS) == {
        "payload",
        "interval_boundary",
        "supersession",
        "deletion",
        "restoration",
    }


def test_corrected_segments_tile_the_original_interval_half_open(store: _Store) -> None:
    """{tbfi}/{BT01}, {ctg0}/{BT11}: complete, non-overlapping, boundary to the later segment."""

    object_id = _document(store)
    created = _created(store, object_id)
    store.advance(days=1)
    store.service.replace_interval(
        assertion_id=created["assertion_id"],
        effective_from=MARCH,
        effective_until=MAY,
        payload_copier=_copy_document_payload,
        payload_writer=_write_document_payload("corrected"),
    )

    accepted = store.service.accepted(object_id=object_id)["items"]
    intervals = [(row["effective_from"], row["effective_until"]) for row in accepted]
    assert intervals == [(JANUARY, MARCH), (MARCH, MAY), (MAY, None)]
    # Contiguity is the tiling: each segment ends exactly where the next begins.
    assert [until for _, until in intervals[:-1]] == [start for start, _ in intervals[1:]]

    known_at = store.clock
    # The boundary instant belongs to the later segment: [from, until).
    at_boundary = select_visible_assertion(store.history(object_id), _pair(MARCH, known_at))
    just_before = select_visible_assertion(
        store.history(object_id), _pair(JUST_BEFORE_MARCH, known_at)
    )
    assert at_boundary["effective_from"] == MARCH
    assert just_before["effective_from"] == JANUARY
    assert at_boundary["assertion_id"] != just_before["assertion_id"]

    # Family payloads follow the segments through the copier/writer seam, in the
    # same transaction, keyed by the assertion UUID and nothing else.
    bodies = {
        row["assertion_id"]: row["body"] for row in store.query("SELECT * FROM document_payloads")
    }
    assert bodies[at_boundary["assertion_id"]] == "corrected"
    assert bodies[just_before["assertion_id"]] == "original"
    assert bodies[accepted[2]["assertion_id"]] == "original"


def test_partial_replacement_supersedes_once_and_omits_empty_segments(store: _Store) -> None:
    """{ctg0}/{BT11}: one global supersession, one correction instant, no empty segment."""

    object_id = _document(store)
    created = _created(store, object_id)
    store.advance(days=1)
    correction_recorded_at = store.clock
    replaced = store.service.replace_interval(
        assertion_id=created["assertion_id"],
        effective_from=JANUARY,
        effective_until=MARCH,
        payload_copier=_copy_document_payload,
        payload_writer=_write_document_payload("corrected"),
    )

    # The window starts where the original did, so there is no left segment.
    assert replaced["left"] is None
    assert replaced["corrected"]["effective_from"] == JANUARY
    assert replaced["corrected"]["effective_until"] == MARCH
    assert replaced["right"]["effective_from"] == MARCH
    assert replaced["right"]["effective_until"] is None

    appended = [replaced["corrected"], replaced["right"]]
    # Both appended segments supersede the one original, at one shared instant.
    assert {row["supersedes_assertion_id"] for row in appended} == {created["assertion_id"]}
    assert {row["recorded_at"] for row in appended} == {correction_recorded_at.isoformat()}
    # The corrected overlap carries the new claim; the untouched remainder keeps
    # the original's kind because it is a copy, not a new claim.
    assert replaced["corrected"]["assertion_kind"] == "supersession"
    assert replaced["right"]["assertion_kind"] == created["assertion_kind"] == "payload"
    # The original survives untouched and is simply no longer accepted.
    original = [
        row for row in store.history(object_id) if row["assertion_id"] == created["assertion_id"]
    ]
    assert original == [created]

    # A window disjoint from the assertion it claims to correct is refused.
    with pytest.raises(TemporalAssertionError) as refusal:
        store.service.replace_interval(
            assertion_id=replaced["corrected"]["assertion_id"],
            effective_from=APRIL,
            effective_until=MAY,
        )
    assert refusal.value.code == "DISJOINT_REPLACEMENT_WINDOW"


def test_appending_an_overlapping_accepted_interval_is_refused(store: _Store) -> None:
    """{8ysr}/{BT10}: the writer will not create a second accepted claim over one instant."""

    object_id = _document(store)
    store.service.create_assertion(
        object_id=object_id,
        effective_from=JANUARY,
        effective_until=MARCH,
        payload_family="document",
    )
    # Adjacent, not overlapping: the half-open interval ends before March begins.
    store.service.create_assertion(
        object_id=object_id,
        effective_from=MARCH,
        effective_until=MAY,
        payload_family="document",
    )
    before = store.history(object_id)

    with pytest.raises(TemporalAssertionError) as refusal:
        store.service.create_assertion(
            object_id=object_id,
            effective_from=FEBRUARY,
            effective_until=APRIL,
            payload_family="document",
        )
    assert refusal.value.code == AMBIGUOUS_EFFECTIVE_INTERVAL
    assert refusal.value.details["conflicting_assertion_id"] in {
        row["assertion_id"] for row in before
    }
    # The refusal is atomic: nothing was appended.
    assert store.history(object_id) == before

    # Ambiguity is scoped to one object and one payload family, so a different
    # family may describe the same instants without colliding.
    other_family = store.service.create_assertion(
        object_id=object_id,
        effective_from=FEBRUARY,
        effective_until=APRIL,
        payload_family="content_attachment",
    )
    assert other_family["outcome"] == "appended"


def test_resolution_rejects_simultaneously_accepted_assertions_instead_of_ranking(
    store: _Store,
) -> None:
    """{8ysr}/{BT10}: an ambiguous instant is a defect the reader reports, not a tie to break."""

    object_id = _document(store)
    created = _created(store, object_id)
    store.advance(days=1)
    # Bypass the writer's guard to model a defect that reached the table: a second
    # accepted assertion covering the same effective instant, superseding nothing.
    store.insert(
        "temporal_assertions",
        assertion_id=uuid4(),
        object_id=object_id,
        payload_family="document",
        assertion_kind="payload",
        revision_no=2,
        effective_from=datetime.fromisoformat(FEBRUARY),
        effective_until=None,
        recorded_at=store.clock,
    )

    rows = store.history(object_id)
    pair = _pair(JULY, store.clock)
    with pytest.raises(AmbiguousAssertionError, match=str(object_id)):
        select_visible_assertion(rows, pair)
    with pytest.raises(AmbiguousAssertionError):
        resolve_visible_versions(rows, pair)

    # Before the second row was recorded the history is unambiguous, which is what
    # makes this a defect of one instant rather than of the whole object.
    earlier = select_visible_assertion(rows, _pair(JULY, FIRST_RECORDED_AT))
    assert earlier["assertion_id"] == created["assertion_id"]


def test_both_times_default_to_one_request_start_instant(store: _Store) -> None:
    """{1vse}/{BT05}, {eh6d}/{BT04}: one clock reading per request, published to the caller."""

    both_omitted = resolve_time_pair()
    assert both_omitted.act_date == both_omitted.known_at
    assert both_omitted.act_date.tzinfo is not None

    request_start = datetime(2027, 6, 1, 12, tzinfo=UTC)
    act_only = resolve_time_pair(datetime.fromisoformat(JANUARY), None, now=request_start)
    assert act_only.act_date == datetime.fromisoformat(JANUARY)
    assert act_only.known_at == request_start
    known_only = resolve_time_pair(None, request_start, now=datetime(2027, 9, 1, tzinfo=UTC))
    assert known_only.known_at == request_start
    assert known_only.act_date == datetime(2027, 9, 1, tzinfo=UTC)
    assert resolve_time_pair(
        datetime.fromisoformat(JANUARY), request_start
    ).as_dict() == {"act_date": JANUARY, "known_at": request_start.isoformat()}

    # Both inputs are optional wherever a versioned read is issued.
    schema = DocumentGetCommand.get_schema()
    assert schema["properties"]["act_date"]["format"] == "date-time"
    assert schema["properties"]["known_at"]["format"] == "date-time"
    assert schema["required"] == ["document_id"]

    # A default pair resolves the object as it stands now, from real rows.
    object_id = _document(store)
    created = _created(store, object_id)
    now = resolve_time_pair(now=datetime(2027, 7, 1, tzinfo=UTC))
    visible = select_visible_assertion(store.history(object_id), now)
    assert visible["assertion_id"] == created["assertion_id"]


def test_each_known_time_resolves_to_the_evidence_accepted_by_then(store: _Store) -> None:
    """{405f}/{BT02}, {eh6d}/{BT04}: a correction changes the answer only from when it was known."""

    object_id = _document(store)
    created = _created(store, object_id)
    before_correction = store.clock
    after_correction = store.advance(days=10)
    store.service.close_interval(assertion_id=created["assertion_id"], effective_until=MARCH)
    rows = store.history(object_id)

    # As of the earlier known time the original open-ended claim is still the
    # answer: the interval boundary exists as a row but is not yet known.
    original = select_visible_assertion(rows, _pair(JULY, before_correction))
    assert original["assertion_id"] == created["assertion_id"]
    assert original["effective_until"] is None
    # As of the later known time the boundary is accepted, so July is outside the
    # closed interval and February is inside it.
    assert select_visible_assertion(rows, _pair(JULY, after_correction)) is None
    boundary = select_visible_assertion(rows, _pair(FEBRUARY, after_correction))
    assert boundary["assertion_kind"] == "interval_boundary"
    assert boundary["supersedes_assertion_id"] == created["assertion_id"]

    # The known-time end of the superseded row is derived from its successor and
    # is deliberately never written back onto it.
    assert rows[0] == created


def test_list_resolution_yields_one_visible_version_per_object_before_ranking(
    store: _Store,
) -> None:
    """{nmu3}/{LC02}, {nwfn}: resolve one version per object, then order, then page."""

    corrected = _document(store)
    plain = _document(store)
    removed = _document(store)
    first = _created(store, corrected)
    _created(store, plain)
    third = _created(store, removed)
    store.advance(days=1)
    store.service.replace_interval(
        assertion_id=first["assertion_id"],
        effective_from=MARCH,
        effective_until=MAY,
        payload_copier=_copy_document_payload,
        payload_writer=_write_document_payload("corrected"),
    )
    store.service.supersede_assertion(assertion_id=third["assertion_id"], effective_until=MARCH)
    store.advance(days=1)
    store.service.soft_delete(object_ids=[removed], effective_from=JANUARY)

    rows = [row for target in (corrected, plain, removed) for row in store.history(target)]
    assert len(rows) > 3
    pair = _pair(APRIL, store.clock)

    resolved = resolve_visible_versions(rows, pair)
    # One entry per object, keyed by the object UUID; the deleted one is absent.
    assert set(resolved) == {str(corrected), str(plain)}
    assert all(row["object_id"] == object_id for object_id, row in resolved.items())
    # Resolution happens before ordering and paging, and does not depend on the
    # order the evidence arrived in.
    assert resolve_visible_versions(list(reversed(rows)), pair) == resolved

    order_key = "effective_from,object_id"
    page = _page(rows, pair, order_key=order_key, position=0, size=10)
    assert page == _page(list(reversed(rows)), pair, order_key=order_key, position=0, size=10)
    assert len(page) == len(set(page)) == 2

    # Version internals never reach the caller: one row, once, per object.
    identities = [row["assertion_id"] for row in resolved.values()]
    assert len(identities) == len(set(identities)) == 2
    assert set(identities) < {row["assertion_id"] for row in rows}


def test_visibility_survives_deletion_and_returns_after_restoration(store: _Store) -> None:
    """{xmk2}/{BT07}, {424z}/{DL02}: deletion removes the present, never the past."""

    object_id = _document(store)
    created = _created(store, object_id)
    before_deletion = store.clock
    between = store.advance(days=5)
    deleted = store.service.soft_delete(object_ids=[object_id])
    deletion = deleted["targets"][0]["deletion"]
    after_restoration = store.advance(days=5)
    restored = store.service.restore(deletion_assertion_id=deletion["assertion_id"])

    rows = store.history(object_id)
    assert [row["assertion_kind"] for row in rows] == ["payload", "deletion", "restoration"]

    # Before the deletion was known, the object is still there.
    was_visible = select_visible_assertion(rows, _pair(JULY, before_deletion))
    assert was_visible["assertion_id"] == created["assertion_id"]
    # Between deletion and restoration the winning assertion is a deletion, so the
    # object is hidden rather than resolved to stale evidence.
    assert select_visible_assertion(rows, _pair(JULY, between)) is None
    # After the restoration it is visible again, through a new assertion.
    now_visible = select_visible_assertion(rows, _pair(JULY, after_restoration))
    assert now_visible["assertion_id"] == restored["assertion"]["assertion_id"]
    assert now_visible["assertion_id"] != created["assertion_id"]

    # The deletion assertion is still there, exactly as it was accepted.
    assert rows[1] == deletion
    assert rows[1]["recorded_at"] == between.isoformat()
    # Restoring something that is not a deletion is refused.
    with pytest.raises(TemporalAssertionError) as refusal:
        store.service.restore(deletion_assertion_id=restored["assertion"]["assertion_id"])
    assert refusal.value.code == "NOT_A_DELETION_ASSERTION"


def test_soft_delete_takes_an_exact_list_with_optional_descendant_closure(store: _Store) -> None:
    """{tox8}/{DL01}: exactly the named UUIDs, plus the registered closure when asked."""

    project = store.register("project", "projects")
    store.insert("projects", id=str(project))
    child = store.register("document", "documents")
    store.insert("documents", id=str(child), owner_id=str(project))
    # A descendant that was never registered cannot be named by an assertion, so
    # the closure drops it instead of inventing an object identity for it.
    store.insert("documents", id=str(uuid4()), owner_id=str(project))

    exact = store.service.soft_delete(object_ids=[project])
    assert exact["requested"] == 1
    assert [target["object_id"] for target in exact["targets"]] == [str(project)]
    assert store.history(child) == []

    store.advance(days=1)
    closure = store.service.soft_delete(object_ids=[child], include_descendants=True)
    assert closure["requested"] == 1
    assert closure["resolved"] == 1

    store.advance(days=1)
    second_project = store.register("project", "projects")
    store.insert("projects", id=str(second_project))
    grandchild = store.register("document", "documents")
    store.insert("documents", id=str(grandchild), owner_id=str(second_project))
    with_closure = store.service.soft_delete(object_ids=[second_project], include_descendants=True)
    assert with_closure["requested"] == 1
    assert with_closure["resolved"] == 2
    assert sorted(target["object_id"] for target in with_closure["targets"]) == sorted(
        [str(second_project), str(grandchild)]
    )

    # An empty list deletes nothing at all rather than defaulting to a wider set.
    assert store.service.soft_delete(object_ids=[]) == {
        "outcome": "deleted",
        "requested": 0,
        "resolved": 0,
        "targets": [],
    }


def test_temporal_deletion_is_not_a_mutable_flag_and_deletes_nothing(store: _Store) -> None:
    """{424z}/{DL02}: append-only evidence, distinct from lifecycle fields and hard delete."""

    object_id = _document(store)
    created = _created(store, object_id)
    store.advance(days=1)
    store.statements.clear()
    deleted = store.service.soft_delete(object_ids=[object_id])
    deletion = deleted["targets"][0]["deletion"]

    # Deletion is an appended assertion superseding the payload it removes.
    assert deletion["assertion_kind"] == "deletion"
    assert deletion["supersedes_assertion_id"] == created["assertion_id"]
    assert deletion["effective_from"] == created["effective_from"]
    assert deleted["targets"][0]["superseded_assertion_id"] == created["assertion_id"]

    # Nothing is removed: not the row, not the registry entry, not the typed row.
    verbs = {statement.split(None, 1)[0].upper() for statement in store.assertion_statements()}
    assert verbs == {"SELECT", "INSERT"}
    assert "DELETE" not in " ".join(store.statements).upper()
    assert len(store.history(object_id)) == 2
    assert store.query(
        "SELECT entity_id FROM entity_uuid_registry WHERE entity_id = :id", id=object_id
    )
    # The mutable entity soft-delete fields of the typed row are untouched: these
    # are two different mechanisms and this one never writes the other's columns.
    typed = store.query("SELECT * FROM documents WHERE id = :id", id=str(object_id))
    assert typed == [
        {
            "id": str(object_id),
            "owner_id": None,
            "block_meta": None,
            "is_deleted": None,
            "deleted_at": None,
        }
    ]
    # The family payload of the superseded assertion survives too, so hard-delete
    # governance stays a separate branch's decision rather than a side effect.
    assert store.query("SELECT * FROM document_payloads") == [
        {"assertion_id": created["assertion_id"], "body": "original"}
    ]


def test_a_stable_cursor_replays_the_identical_page(store: _Store) -> None:
    """{q52o}/{LC03}: the pinned pair, order and identity reproduce the page exactly."""

    first = _document(store)
    second = _document(store)
    _created(store, first)
    _created(store, second)
    pinned_at = store.clock
    pair = _pair(APRIL, pinned_at)
    order_key = "effective_from,object_id"
    query = ChunkQuery(type="DocBlock", project="doc-store")

    cursor = TemporalCursor.pin(
        act_date=pair.act_date.isoformat(),
        known_at=pair.known_at.isoformat(),
        order_key=order_key,
        query=query,
        position=0,
    )
    token = cursor.encode()
    assert TemporalCursor.decode(token) == cursor
    assert cursor.resolved_pair == (pair.act_date.isoformat(), pair.known_at.isoformat())
    assert cursor.integrity == TemporalCursor.decode(token).integrity

    rows = [row for target in (first, second) for row in store.history(target)]
    page = _page(rows, pair, order_key=order_key, position=cursor.position, size=1)
    assert len(page) == 1

    # New evidence recorded after the cursor was pinned does not disturb the page
    # it pins, because the cursor replays the resolved pair rather than "now".
    store.advance(days=1)
    store.service.soft_delete(object_ids=[first, second])
    replayed = TemporalCursor.decode(token)
    replayed.validate_replay(
        act_date=pair.act_date.isoformat(),
        known_at=pair.known_at.isoformat(),
        order_key=order_key,
        query=query,
    )
    later_rows = [row for target in (first, second) for row in store.history(target)]
    pinned_pair = _pair(
        replayed.act_date, datetime.fromisoformat(replayed.known_at)
    )
    assert _page(later_rows, pinned_pair, order_key=replayed.order_key, position=0, size=1) == page
    # Resolving at "now" instead would answer differently, which is the point.
    assert _page(later_rows, _pair(APRIL, store.clock), order_key=order_key, position=0, size=1) == []

    # Paging keeps the pinned state and re-seals the integrity hash over the new
    # position, and the second page continues where the first stopped.
    following = replayed.advance(1)
    assert following.resolved_pair == replayed.resolved_pair
    assert following.query_id == replayed.query_id
    assert following.integrity != replayed.integrity
    second_page = _page(later_rows, pinned_pair, order_key=order_key, position=1, size=1)
    assert second_page and second_page != page

    # Temporal bounds ride on the query without changing its identity, so turning
    # a page never looks like a different search.
    bounded = with_temporal_bounds(query, act_date=APRIL, known_at=pinned_at.isoformat())
    assert canonical_query_id(bounded) == canonical_query_id(query) == cursor.query_id


def test_cursor_replay_rejects_a_mismatched_pair_ordering_or_query_identity() -> None:
    """{q52o}/{LC03}: a cursor is only valid for what it pins, and it is sealed."""

    query = ChunkQuery(type="DocBlock", project="doc-store")
    cursor = TemporalCursor.pin(
        act_date=APRIL,
        known_at=JULY,
        order_key="effective_from,object_id",
        query=query,
        position=0,
    )

    for kwargs, expected in (
        ({"act_date": MARCH}, "act_date"),
        ({"known_at": MAY}, "known_at"),
        ({"order_key": "relevance"}, "order_key"),
        ({"query": ChunkQuery(type="DocBlock", project="other")}, "query_id"),
    ):
        with pytest.raises(ValueError, match=expected):
            cursor.validate_replay(**kwargs)

    # The same instant spelled differently is the same instant, not a mismatch.
    cursor.validate_replay(act_date="2026-04-01T02:00:00+02:00", known_at=JULY)

    # The integrity hash is over the pinned state, so a rewritten position, pair,
    # ordering or query identity no longer verifies.
    for tampered in (
        {"position": 4},
        {"act_date": MARCH},
        {"order_key": "relevance"},
        {"query_id": "0" * 64},
    ):
        payload = dict(cursor.to_params())
        payload.update(tampered)
        with pytest.raises(ValueError, match="integrity"):
            TemporalCursor.from_payload(payload)
    with pytest.raises(ValueError, match="not a valid cursor token"):
        TemporalCursor.decode("not-a-cursor")

    # A request may repeat the bounds next to a cursor only if they agree with it.
    request_cursor = TemporalCursor.pin(
        act_date=APRIL,
        known_at=JULY,
        order_key="effective_from,object_id",
        query=TemporalRequest(act_date=APRIL, known_at=JULY),
        position=0,
    )
    token = request_cursor.encode()
    assert TemporalRequest(act_date=APRIL, known_at=JULY, cursor=token).resolved_pair() == (
        APRIL,
        JULY,
    )
    with pytest.raises(ValueError, match="act_date"):
        TemporalRequest(act_date=MARCH, known_at=JULY, cursor=token)


def test_semantic_chunk_current_pointers_are_not_temporal_authority(store: _Store) -> None:
    """{j7vu}/{ID08}: one version identity; a mutable current flag cannot stand in for it."""

    # The chunk-version mechanism keeps its own current pointer and validity
    # columns; the shared assertion has none of them and does not reference it.
    pointer_columns = set(SemanticChunkVersion.__table__.c.keys())
    assert {"is_current", "valid_from", "valid_to", "version_no"} <= pointer_columns
    assert not pointer_columns & {"effective_from", "effective_until", "recorded_at"}
    assert "semantic_chunk_versions" not in _offline_sql(_load_migration(ASSERTION_MIGRATION))

    # A chunk-version row cannot even be read as temporal evidence.
    chunk_version = {
        "id": str(uuid4()),
        "logical_chunk_id": str(uuid4()),
        "version_no": 3,
        "is_current": True,
        "valid_from": JANUARY,
        "valid_to": None,
    }
    assert is_assertion_like(chunk_version) is False

    # And resolution ignores such a pointer when it rides along on real evidence:
    # the superseded row stays superseded however current it claims to be.
    object_id = _document(store)
    created = _created(store, object_id)
    store.advance(days=1)
    store.service.close_interval(assertion_id=created["assertion_id"], effective_until=JULY)
    rows = store.history(object_id)
    assert is_assertion_like(rows[0]) is True
    rows[0]["is_current"] = True
    rows[1]["is_current"] = False
    visible = select_visible_assertion(rows, _pair(FEBRUARY, store.clock))
    assert visible["assertion_id"] == rows[1]["assertion_id"]
    assert visible["assertion_kind"] == "interval_boundary"
