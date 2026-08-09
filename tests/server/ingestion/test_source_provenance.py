"""Focused regressions: immutable source lineage, auditable runs, hierarchy provenance.

G-003 owns the evidence chain PrimarySource -> ProcessingRun -> SourceState ->
Document/Chapter/Paragraph. This module proves the properties that chain has to
have, against the code that actually implements it: migration
``0019_source_provenance``, the three declarations in ``db/schema.py``, the
provenance helpers of ``ingestion/runtime_boundary.py`` and the single
transaction ``PublicationRepository.publish_transaction`` owns.

What is proved here:

* **Immutable original** ({5434}/{SO01}). ``primary_sources`` refuses every
  UPDATE, a File states exactly one original through a NOT NULL, unique,
  ON DELETE RESTRICT column installed only after a verified backfill, and the
  hash the write path records is the hash of the *original* bytes. Re-ingesting
  a File that already names an original reuses it instead of minting a second.
* **Auditable run** ({hd4e}/{SO03}/{0o1l}). One run row carries the input
  original, the deterministic configuration hash, the immutable normalization
  profile ID/version/parameters, the actor, the status, both timestamps and the
  typed document and chapter outputs. Its round trip is *measured* against the
  rows just written, and the ``succeeded`` status is gated by a check constraint
  that is lifted from the rendered DDL and executed, so success is shown to be
  unrepresentable without two equal hashes rather than merely asserted.
* **Appended source state** ({73a1}/{SO02}/{ik6b}). The state is a
  ``source_state`` payload of the shared ``TemporalAssertion``: the first is a
  ``payload`` assertion, a republish appends a ``supersession`` naming its
  predecessor, and neither the write path nor the migration ever updates or
  deletes an accepted state.
* **Hierarchy lineage** ({5vcr}/{xmpp}). Every produced Document, Chapter,
  Paragraph and chunk carries the source-state assertion and the producing run,
  and a half-stated pair is refused rather than half-recorded.
* **Idempotency and rollback** ({xmpp}/{IN01}). A replay answers from what is
  stored without writing anything, and a failure anywhere in the publication
  leaves one rolled-back transaction: no hierarchy, and no provenance.

Everything is offline and deterministic. Migrations are rendered with alembic's
``as_sql`` mode exactly as ``tests/test_entity_uuid_registry.py`` and
``tests/test_content_attachment_temporal_state.py`` do; the two plain-SQL run
gates are lifted verbatim out of the rendered DDL and executed against an
in-memory SQLite fixture; and the write path runs against a recording connection
that answers the reconstruction query from the chunk payloads the same
transaction just inserted, so the round-trip verdict is derived rather than
stubbed. What genuinely needs PostgreSQL --- plpgsql trigger dispatch,
``RAISE EXCEPTION`` SQLSTATEs, real referential actions --- is asserted against
the emitted function bodies and DDL instead of skipped.

Three assertions here were superseded by G-003/T-006/A-006, which turned a
non-matching round trip into a refusal and split publication into an evidence
prologue, a hierarchy transaction and a refusal-evidence transaction. Each is
updated in place at its site with the reason recorded there, and nothing else in
this module changed: publication no longer returns a reference dictionary for a
mismatching round trip (it raises ``IntegrityRefused``, and appends no
``SourceState``), and the two ``engine.blocks == 1`` counts became two blocks
because the immutable original is now committed on its own before the hierarchy
that may roll back. The behaviour that replaced the stale part --- the rolled
back hierarchy, the committed failed run and its ``SourceSpan``, and the
unchanged matching path --- is proved in ``test_integrity_refusal``.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from doc_store_server.db.schema import (
    INTEGRITY_OUTCOMES,
    PROCESSING_RUN_KINDS,
    PROCESSING_RUN_STATUSES,
    REGISTRY_KINDS,
    SOURCE_STATE_PAYLOAD_FAMILY,
    File,
    PrimarySource,
    ProcessingRun,
    SourceState,
    metadata,
)
from doc_store_server.domain.models import Chapter, Document, Paragraph
from doc_store_server.domain.semantic_chunk import ChunkSpec, build_semantic_chunks
from doc_store_server.ingestion import integrity_diagnostics, runtime_boundary
from doc_store_server.ingestion.hierarchy_enrichment import (
    ChapterInput,
    HierarchyEnrichmentError,
    ParagraphInput,
    enrich_hierarchy,
)
from doc_store_server.ingestion.persistence_plan import PersistencePlan
from doc_store_server.ingestion.publication import (
    IdempotentReplay,
    RolledBackPublication,
    publish_document,
)
from doc_store_server.ingestion.publication_repository import PublicationRepository
from doc_store_server.ingestion.svo_chunking import RuntimeChunk
from doc_store_server.runtime.exact_reconstruction import EXACT_RECONSTRUCTION_KEY


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "migrations" / "versions"
PROVENANCE_MIGRATION = "0019_source_provenance"

DATABASE_URL = "postgresql://source-provenance/unused"

DOCUMENT_ID = UUID("0c012000-0000-4000-8000-000000000001")
FILE_ID = UUID("0c012000-0000-4000-8000-0000000000f1")
PARAGRAPH_CHUNK_ID = UUID("0c017000-0000-4000-8000-00000000000a")
FIRST_SENTENCE_ID = UUID("0c017000-0000-4000-8000-00000000000b")
SECOND_SENTENCE_ID = UUID("0c017000-0000-4000-8000-00000000000c")
PRIOR_ASSERTION_ID = UUID("73a10000-0000-4000-8000-000000000001")
OPERATION_ID = "0c014000-0000-4000-8000-000000000009"

SOURCE_TEXT = "First sentence. Second sentence."
FIRST_SENTENCE = "First sentence."
SECOND_SENTENCE = "Second sentence."
SOURCE_SHA256 = hashlib.sha256(SOURCE_TEXT.encode("utf-8")).hexdigest()
ORIGINAL_SHA256 = "a" * 64

RUN_ACTOR = "doc-store:tests:source-provenance"


# ----------------------------------------------------------------------
# Rendering the migration offline
# ----------------------------------------------------------------------


def _load_migration(name: str) -> ModuleType:
    path = MIGRATIONS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provenance_migration() -> ModuleType:
    return _load_migration(PROVENANCE_MIGRATION)


def _offline_sql(module: ModuleType, function: str = "upgrade") -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        getattr(module, function)()
    return output.getvalue()


def _upgrade_sql() -> str:
    return _offline_sql(_provenance_migration())


def _squeeze(sql: str) -> str:
    """Collapse the rendered layout so assertions read as SQL, not as whitespace."""

    return " ".join(sql.split())


def _statement(sql: str, opening: str) -> str:
    """Return the one rendered statement beginning with ``opening``."""

    matches = [
        chunk.strip()
        for chunk in re.split(r"(?m)^(?=CREATE |ALTER |DO )", sql)
        if chunk.strip().startswith(opening)
    ]
    assert len(matches) == 1, f"expected exactly one {opening!r} statement, got {len(matches)}"
    return matches[0]


def _check_predicate(sql: str, table: str, name: str) -> str:
    """Lift one named CHECK predicate verbatim out of the rendered CREATE TABLE."""

    create = _squeeze(_statement(sql, f"CREATE TABLE {table}"))
    marker = f"CONSTRAINT ck_{table}_{name} CHECK ("
    start = create.index(marker) + len(marker)
    depth = 1
    end = start
    while depth:
        if create[end] == "(":
            depth += 1
        elif create[end] == ")":
            depth -= 1
            if depth == 0:
                break
        end += 1
    return create[start:end]


# ----------------------------------------------------------------------
# A recording connection and engine: the write path runs, nothing connects
# ----------------------------------------------------------------------


class _FakeResult:
    """The three result shapes the provenance write path asks a connection for."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = [dict(row) for row in rows]

    def _first(self) -> Any:
        return next(iter(self._rows[0].values()))

    def scalar_one_or_none(self) -> Any:
        return self._first() if self._rows else None

    def scalar_one(self) -> Any:
        assert len(self._rows) == 1, "scalar_one over a non-singleton result"
        return self._first()

    def scalars(self) -> list[Any]:
        return [next(iter(row.values())) for row in self._rows]

    def mappings(self) -> "_FakeResult":
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def one_or_none(self) -> dict[str, Any] | None:
        assert len(self._rows) <= 1, "one_or_none over a multi-row result"
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """Record every statement, and answer the few reads the write path makes.

    The reconstruction query is deliberately answered from the chunk payloads
    this same connection was asked to insert, so the round-trip verdict the run
    records is derived from what was written rather than handed to it.
    """

    def __init__(
        self,
        *,
        existing_primary_source_id: UUID | None = None,
        registered_document: UUID | None = DOCUMENT_ID,
        prior_assertion_id: UUID | None = None,
        existing_chunk_ids: Sequence[UUID] = (),
        document_state: Mapping[str, Any] | None = None,
        committed_document: Mapping[str, Any] | None = None,
        reconstruction: str | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self._existing_primary_source_id = existing_primary_source_id
        self._registered_document = registered_document
        self._prior_assertion_id = prior_assertion_id
        self._existing_chunk_ids = tuple(existing_chunk_ids)
        self._document_state = document_state
        self._committed_document = committed_document
        self._reconstruction = reconstruction
        self._fail_on = fail_on
        self._chunk_paragraphs: dict[str, str] = {}
        self._chunk_metas: dict[str, Mapping[str, Any]] = {}
        self._chunk_texts: list[tuple[str, str, Mapping[str, Any]]] = []
        self.savepoints: list[_FakeSavepoint] = []

    # -- savepoints ------------------------------------------------------

    def begin_nested(self) -> "_FakeSavepoint":
        """Open a SAVEPOINT, exactly as the real connection does.

        Publication writes the hierarchy inside one so that a refusal can
        discard it without leaving the transaction, which is what lets the
        refusal evidence be committed by the same transaction that discarded
        it. A fake that ignored savepoints would make that indistinguishable
        from writing the hierarchy and keeping it.
        """

        savepoint = _FakeSavepoint(self, start=len(self.statements))
        self.savepoints.append(savepoint)
        return savepoint

    # -- recorded statements -------------------------------------------

    def issued(self, fragment: str) -> list[dict[str, Any]]:
        """Every matching statement the connection was asked to run.

        Includes statements a savepoint later discarded; use ``committed`` for
        what the transaction would actually persist.
        """

        return [params for sql, params in self.statements if fragment in sql]

    def committed(self, fragment: str) -> list[dict[str, Any]]:
        """Matching statements that survive every savepoint rollback.

        Savepoints only. The connection does not know whether the enclosing
        transaction committed, so a statement outside a rolled-back savepoint
        appears here even if its whole block was later lost; ask the engine for
        that. Use this to reason about what a savepoint discarded, not about
        durability.
        """

        discarded = set()
        for savepoint in self.savepoints:
            if savepoint.outcome == "rollback":
                discarded.update(range(savepoint.start, savepoint.end))
        return [
            params
            for index, (sql, params) in enumerate(self.statements)
            if fragment in sql and index not in discarded
        ]

    def one(self, fragment: str) -> dict[str, Any]:
        matches = self.issued(fragment)
        assert len(matches) == 1, f"expected exactly one {fragment!r}, got {len(matches)}"
        return matches[0]

    def order_of(self, *fragments: str) -> list[int]:
        positions = []
        for fragment in fragments:
            found = [index for index, (sql, _) in enumerate(self.statements) if fragment in sql]
            assert found, f"no statement matching {fragment!r} was issued"
            positions.append(found[0])
        return positions

    # -- the connection surface ----------------------------------------

    def execute(self, statement: Any, params: Mapping[str, Any] | None = None) -> _FakeResult:
        sql = _squeeze(str(statement))
        values = dict(params or {})
        self.statements.append((sql, values))
        if self._fail_on is not None and self._fail_on in sql:
            raise RuntimeError(f"storage refused: {self._fail_on}")
        return _FakeResult(self._rows_for(sql, values))

    def _rows_for(self, sql: str, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        if sql.startswith("INSERT INTO semantic_chunks "):
            self._chunk_paragraphs[str(values["id"])] = str(values["paragraph_id"])
            block_meta = json.loads(str(values.get("block_meta") or "{}"))
            self._chunk_metas[str(values["id"])] = block_meta if isinstance(block_meta, Mapping) else {}
            return []
        if sql.startswith("INSERT INTO semantic_chunk_texts "):
            block_meta = json.loads(str(values.get("block_meta") or "{}"))
            self._chunk_texts.append(
                (
                    str(values["chunk_uuid"]),
                    str(values["body"]),
                    block_meta if isinstance(block_meta, Mapping) else {},
                )
            )
            return []
        if sql.startswith("SELECT primary_source_id FROM files"):
            if self._existing_primary_source_id is None:
                return []
            return [{"primary_source_id": self._existing_primary_source_id}]
        if sql.startswith("SELECT entity_id FROM entity_uuid_registry"):
            if self._registered_document is None:
                return []
            return [{"entity_id": self._registered_document}]
        if sql.startswith("SELECT a.assertion_id FROM temporal_assertions"):
            if self._prior_assertion_id is None:
                return []
            return [{"assertion_id": self._prior_assertion_id}]
        if sql.startswith("SELECT id FROM semantic_chunks"):
            return [{"id": chunk_id} for chunk_id in self._existing_chunk_ids]
        if sql.startswith("SELECT d.needs_revectorize"):
            return [dict(self._document_state)] if self._document_state else []
        if "FROM documents WHERE id = :document_id" in sql:
            return [dict(self._committed_document)] if self._committed_document else []
        if sql.startswith("SELECT sc.paragraph_id::text") or sql.startswith("SELECT sc.id::text"):
            return self._reconstruction_rows()
        if re.match(r"SELECT id FROM \w+ WHERE descr", sql):
            return []
        if "RETURNING id" in sql or sql.startswith("WITH inserted AS"):
            return [{"id": runtime_boundary._stable_uuid4(sql + json.dumps(values, default=str))}]
        return []

    def _reconstruction_rows(self) -> list[dict[str, Any]]:
        """Rebuild the compared scope from the chunk payloads actually inserted."""

        rows = [
            {
                "chunk_id": chunk_id,
                "paragraph_id": self._chunk_paragraphs[chunk_id],
                "text": body,
                "chunk_block_meta": self._chunk_metas.get(chunk_id, {}),
                "text_block_meta": text_meta,
            }
            for chunk_id, body, text_meta in self._chunk_texts
        ]
        if self._reconstruction is None:
            return rows
        # A deliberately lossy store: the whole scope is replaced by one payload
        # so a non-matching round trip can be observed end to end.
        paragraph_id = rows[0]["paragraph_id"] if rows else str(DOCUMENT_ID)
        return [
            {
                "chunk_id": "lossy-chunk",
                "paragraph_id": paragraph_id,
                "text": self._reconstruction,
                "chunk_block_meta": {},
                "text_block_meta": {},
            }
        ]


class _FakeSavepoint:
    """One SAVEPOINT, and whether it was released or rolled back to."""

    def __init__(self, connection: _FakeConnection, *, start: int) -> None:
        self._connection = connection
        self.start = start
        self.end: int | None = None
        self.outcome: str | None = None

    def _close(self, outcome: str) -> None:
        assert self.outcome is None, "savepoint resolved twice"
        self.end = len(self._connection.statements)
        self.outcome = outcome

    def rollback(self) -> None:
        self._close("rollback")

    def commit(self) -> None:
        self._close("commit")


class _FakeTransaction:
    def __init__(self, engine: "_FakeEngine") -> None:
        self._engine = engine

    def __enter__(self) -> _FakeConnection:
        self._engine.blocks += 1
        return self._engine.connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self._engine.outcomes.append("rollback" if exc_type is not None else "commit")
        return False


class _FakeEngine:
    """One connection, and a record of whether each block committed or rolled back."""

    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.blocks = 0
        self.outcomes: list[str] = []
        self.disposed = 0

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    def connect(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    def dispose(self) -> None:
        self.disposed += 1


def _install_engine(
    monkeypatch: pytest.MonkeyPatch, connection: _FakeConnection
) -> _FakeEngine:
    engine = _FakeEngine(connection)
    monkeypatch.setattr(PublicationRepository, "_engine", lambda _self: engine)
    monkeypatch.setenv(runtime_boundary.RUN_ACTOR_ENV, RUN_ACTOR)
    return engine


# ----------------------------------------------------------------------
# The payload and the chunk units one publication writes
# ----------------------------------------------------------------------


def _plan(**overrides: Any) -> PersistencePlan:
    values: dict[str, Any] = {
        "document_id": DOCUMENT_ID,
        "source_version_id": "source-v1",
        "normalized_source_version_id": "normalized-v1",
        "operation_id": OPERATION_ID,
        "command": "document_create",
        "text_value": SOURCE_TEXT,
        "filename": "chapter-one.md",
        "content_sha256": ORIGINAL_SHA256,
        "chunking_strategy": "paragraph",
        "source_version": 1,
        "title": "Chapter one",
        "source_name": "chapter-one.md",
        "length": len(SOURCE_TEXT),
        "body_sha256": SOURCE_SHA256,
        "file_id": FILE_ID,
        "doc_meta": {"source_version_id": "source-v1"},
        "media_type": "text/markdown",
        "byte_length": len(SOURCE_TEXT.encode("utf-8")),
    }
    values.update(overrides)
    return PersistencePlan(**values)


def _chunk_units() -> tuple[tuple[RuntimeChunk, ...], tuple[RuntimeChunk, ...]]:
    paragraph = RuntimeChunk(
        uuid=PARAGRAPH_CHUNK_ID,
        text=SOURCE_TEXT,
        start=0,
        end=len(SOURCE_TEXT),
        ordinal=0,
        metadata={},
    )
    sentences = (
        RuntimeChunk(
            uuid=FIRST_SENTENCE_ID,
            text=FIRST_SENTENCE,
            start=0,
            end=len(FIRST_SENTENCE),
            ordinal=0,
            metadata={},
        ),
        RuntimeChunk(
            uuid=SECOND_SENTENCE_ID,
            text=SECOND_SENTENCE,
            start=len(FIRST_SENTENCE) + 1,
            end=len(SOURCE_TEXT),
            ordinal=1,
            metadata={},
        ),
    )
    return (paragraph,), sentences


class _FakeChunker:
    """The paragraph and its two sentences, without an SVO service."""

    async def chunk(
        self, *, text: str, strategy: str, source_id: str
    ) -> tuple[RuntimeChunk, ...]:
        return _chunk_units()[0]

    async def chunk_batch(
        self, *, texts: list[str], strategy: str, source_ids: list[str]
    ) -> tuple[tuple[RuntimeChunk, ...], ...]:
        return tuple(_chunk_units()[1] for _ in texts)


def _repository() -> PublicationRepository:
    """The production repository, with the only external service replaced."""

    return PublicationRepository(DATABASE_URL, chunker=_FakeChunker())


def _publish(
    connection: _FakeConnection, plan: PersistencePlan | None = None
) -> dict[str, Any]:
    paragraph_chunks, sentence_chunks = _chunk_units()
    return asyncio.run(
        _repository().publish_transaction(
            plan or _plan(), paragraph_chunks, sentence_chunks
        )
    )


# ----------------------------------------------------------------------
# 1. The immutable original, and the File that states exactly one of them
# ----------------------------------------------------------------------


def test_primary_source_is_immutable_and_a_file_states_exactly_one_original() -> None:
    """{5434}/{SO01}: originals are never rewritten, and a File cannot lose its own."""

    migration = _provenance_migration()
    sql = _upgrade_sql()

    trigger = _squeeze(_statement(sql, f"CREATE TRIGGER {migration.SOURCE_IMMUTABLE_TRIGGER}"))
    assert trigger == (
        f"CREATE TRIGGER {migration.SOURCE_IMMUTABLE_TRIGGER}"
        f" BEFORE UPDATE ON {migration.SOURCE_TABLE}"
        f" FOR EACH ROW EXECUTE FUNCTION {migration.SOURCE_IMMUTABLE_FUNCTION}();"
    )
    # Unconditional: no column ladder, no exemption -- every UPDATE is refused.
    guard = _squeeze(
        _statement(sql, f"CREATE OR REPLACE FUNCTION {migration.SOURCE_IMMUTABLE_FUNCTION}()")
    )
    assert "IS DISTINCT FROM" not in guard
    assert "RETURN NEW" not in guard
    assert "ERRCODE = 'restrict_violation'" in guard
    assert f"'{migration.SOURCE_TABLE} is immutable: original '" in guard
    assert "A correction is a new PrimarySource with a new UUID" in guard

    # Nothing in the revision rewrites or removes an original either.
    assert f"UPDATE {migration.SOURCE_TABLE}" not in sql
    assert f"DELETE FROM {migration.SOURCE_TABLE}" not in sql

    # The exactly-one association is staged, never asserted over unbacked rows:
    # preflight before any DDL, column added nullable, backfill, verification,
    # and only then NOT NULL, unique and the RESTRICT foreign key.
    positions = [
        sql.index("refuses to proceed"),
        sql.index(f"CREATE TABLE {migration.SOURCE_TABLE}"),
        sql.index(f"ALTER TABLE {migration.FILE_TABLE} ADD COLUMN {migration.FILE_SOURCE_COLUMN}"),
        sql.index(f"INSERT INTO {migration.SOURCE_TABLE} ("),
        sql.index("backfill left"),
        sql.index(f"ALTER COLUMN {migration.FILE_SOURCE_COLUMN} SET NOT NULL"),
        sql.index(f"ADD CONSTRAINT uq_{migration.FILE_TABLE}_{migration.FILE_SOURCE_COLUMN} UNIQUE"),
        sql.index(
            f"ADD CONSTRAINT fk_{migration.FILE_TABLE}_{migration.FILE_SOURCE_COLUMN}"
            f"_{migration.SOURCE_TABLE} FOREIGN KEY"
        ),
    ]
    assert positions == sorted(positions)
    assert _squeeze(
        _statement(sql, f"ALTER TABLE {migration.FILE_TABLE} ADD CONSTRAINT fk_")
    ).endswith("REFERENCES primary_sources (id) ON DELETE RESTRICT;")

    # The backfill states what it does not know rather than guessing an encoding.
    backfill = _squeeze(migration._backfill_sql())
    assert "'recorded_encoding_known', false" in backfill
    assert "'captured_by', '0019_source_provenance'" in backfill
    assert "p.original_sha256" in backfill

    # And the mapping says the same thing the migration installs.
    column = File.__table__.c[migration.FILE_SOURCE_COLUMN]
    assert column.nullable is False
    (foreign_key,) = column.foreign_keys
    assert foreign_key.target_fullname == f"{migration.SOURCE_TABLE}.id"
    assert foreign_key.ondelete == "RESTRICT"
    assert any(
        constraint.name == f"uq_{migration.FILE_TABLE}_{migration.FILE_SOURCE_COLUMN}"
        and [item.name for item in constraint.columns] == [migration.FILE_SOURCE_COLUMN]
        for constraint in File.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )


def test_the_original_hash_is_of_the_original_bytes_and_is_minted_once_per_file() -> None:
    """{5434}: normalization never replaces the original, and a replay reuses it."""

    plan = _plan()
    fresh = _FakeConnection()
    source_id = runtime_boundary._resolve_primary_source(fresh, plan)

    insert = fresh.one("INSERT INTO primary_sources")
    # The recorded hash is the *original*-byte digest carried on the plan, which
    # is not the digest of the normalized source the run compares.
    assert insert["original_sha256"] == ORIGINAL_SHA256
    assert insert["original_sha256"] != plan.body_sha256
    assert insert["id"] == source_id
    assert insert["source_locator"] == plan.filename
    assert insert["media_type"] == plan.media_type
    assert insert["byte_length"] == plan.byte_length
    assert insert["recorded_encoding"] == runtime_boundary.NORMALIZED_SOURCE_ENCODING
    provenance = json.loads(insert["provenance"])
    assert provenance["file_id"] == str(FILE_ID)
    assert provenance["document_id"] == str(DOCUMENT_ID)
    assert provenance["ingestion_command"] == plan.command
    assert provenance["operation_id"] == OPERATION_ID

    # The identity is derived from the File and its original bytes, so the same
    # File re-offered with the same bytes resolves to the same original.
    assert source_id == runtime_boundary._stable_uuid4(
        f"doc-store:primary-source:{FILE_ID}:{ORIGINAL_SHA256}"
    )
    assert source_id.version == 4
    assert runtime_boundary._resolve_primary_source(_FakeConnection(), plan) == source_id

    # A File that already names an original keeps it and mints nothing: an
    # original is immutable, and re-ingesting is not a reason to fork the chain.
    existing = UUID("5434dead-0000-4000-8000-000000000001")
    bound = _FakeConnection(existing_primary_source_id=existing)
    assert runtime_boundary._resolve_primary_source(bound, plan) == existing
    assert bound.issued("INSERT INTO primary_sources") == []

    # A source with neither filename nor name still has a findable locator.
    anonymous = _FakeConnection()
    runtime_boundary._resolve_primary_source(
        anonymous, _plan(filename=None, source_name=None)
    )
    locator = anonymous.one("INSERT INTO primary_sources")["source_locator"]
    assert locator == f"doc-store:document:{DOCUMENT_ID}:source-v1"
    assert 0 < len(locator) <= 2048


# ----------------------------------------------------------------------
# 2. The auditable run and its measured round trip
# ----------------------------------------------------------------------


def test_publication_records_one_auditable_run_over_a_measured_matching_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{hd4e}/{SO03}/{0o1l}: inputs, configuration, actor, status, times, outputs."""

    connection = _FakeConnection()
    engine = _install_engine(monkeypatch, connection)

    references = _publish(connection)

    # Superseded by G-003/T-006/A-006: publication now opens an evidence prologue
    # that commits the immutable original alone before the hierarchy block, so a
    # matching publication runs two blocks rather than one. The claim this test
    # makes is unchanged --- the hierarchy, the run and the state are one
    # committed transaction --- and it is stated below over the block that holds
    # them; only the count of blocks in the whole publication moved.
    assert engine.blocks == 2
    assert engine.outcomes == ["commit", "commit"]
    assert engine.disposed == 1

    run = connection.one("INSERT INTO processing_runs")
    plan = _plan()

    # Input: the immutable original the File is bound to, and no other.
    original = connection.one("INSERT INTO primary_sources")
    assert run["primary_source_id"] == original["id"]

    # Configuration: hash plus the immutable profile identity and full parameters.
    parameters = runtime_boundary._normalization_parameters(plan)
    assert run["configuration_hash"] == runtime_boundary._configuration_hash(
        plan.command, parameters
    )
    assert run["configuration_hash"] == runtime_boundary._configuration_hash(
        plan.command, runtime_boundary._normalization_parameters(plan)
    )
    # Spelled out as well as referenced: a silent rename of the profile the
    # deterministic configuration is identified by has to fail here.
    assert run["profile_id"] == runtime_boundary.NORMALIZATION_PROFILE_ID
    assert run["profile_id"] == "doc-store.ingestion.runtime_boundary"
    assert run["profile_version"] == runtime_boundary.NORMALIZATION_PROFILE_VERSION
    assert run["profile_version"] == "1"
    recorded_parameters = json.loads(run["parameters"])
    assert recorded_parameters == parameters
    assert recorded_parameters["chunking_strategy"] == plan.chunking_strategy
    assert recorded_parameters["reconstruction_separators"] == dict(
        runtime_boundary.RECONSTRUCTION_SEPARATORS
    )
    assert recorded_parameters["normalized_source_version_id"] == (
        plan.normalized_source_version_id
    )

    # Actor, kind and scope.
    assert run["actor"] == RUN_ACTOR
    assert run["run_kind"] == "document_create"
    assert run["run_kind"] in PROCESSING_RUN_KINDS
    assert run["comparison_scope"] == runtime_boundary.COMPARISON_SCOPE
    assert run["comparison_scope"] == "document_normalized_source"

    # Status and verdict: measured against the rows this transaction wrote.
    reconstructed = FIRST_SENTENCE + " " + SECOND_SENTENCE
    assert reconstructed == SOURCE_TEXT
    assert run["reconstruction_sha256"] == hashlib.sha256(
        reconstructed.encode("utf-8")
    ).hexdigest()
    assert run["source_sha256"] == plan.body_sha256 == run["reconstruction_sha256"]
    assert run["integrity_outcome"] == "match"
    assert run["status"] == "succeeded"
    assert run["status"] in PROCESSING_RUN_STATUSES
    assert run["first_difference_offset"] is None
    assert run["source_encoding"] == run["reconstructed_encoding"] == "utf-8"

    public_chunk_meta = [
        json.loads(params["block_meta"])
        for params in connection.issued("INSERT INTO semantic_chunks ")
    ]
    internal_text_meta = [
        json.loads(params["block_meta"])
        for params in connection.issued("INSERT INTO semantic_chunk_texts ")
    ]
    assert all(EXACT_RECONSTRUCTION_KEY not in metadata for metadata in public_chunk_meta)
    assert internal_text_meta[0][EXACT_RECONSTRUCTION_KEY] == {
        "version": 1,
        "gap_before": "",
    }
    assert internal_text_meta[1][EXACT_RECONSTRUCTION_KEY] == {
        "version": 1,
        "gap_before": " ",
        "suffix_after": "",
    }

    # Timestamps: ordered, and correlated with the request that caused them.
    assert run["started_at"] <= run["completed_at"]
    assert run["operation_id"] == OPERATION_ID

    # Typed outputs: the document and the chapter this run produced.
    chapter = connection.one("INSERT INTO chapters")
    assert run["document_id"] == DOCUMENT_ID
    assert run["chapter_id"] == chapter["id"]

    # The run is written once, at the end of the execution it describes, and is
    # never revisited: an audit identity that could drift is not audit evidence.
    assert len(connection.issued("INSERT INTO processing_runs")) == 1
    assert connection.issued("UPDATE processing_runs") == []
    assert connection.issued("DELETE FROM processing_runs") == []

    # Provenance is written after the hierarchy it accounts for, in one block.
    positions = connection.order_of(
        "INSERT INTO primary_sources",
        "INSERT INTO files",
        "INSERT INTO documents",
        "INSERT INTO chapters",
        "INSERT INTO paragraphs",
        "INSERT INTO semantic_chunks ",
        "INSERT INTO processing_runs",
        "INSERT INTO source_states",
    )
    assert positions == sorted(positions)

    # The File row names that original, and the reference exposes the chain.
    assert connection.one("INSERT INTO files")["primary_source_id"] == original["id"]
    assert references["primary_source_id"] == str(original["id"])
    assert references["processing_run_id"] == str(run["run_id"])
    assert references["processing_run_status"] == "succeeded"
    assert references["integrity_outcome"] == "match"
    assert references["source_state_assertion_id"] == str(
        connection.one("INSERT INTO source_states")["assertion_id"]
    )
    assert references["idempotent"] is False


def test_success_is_unrepresentable_without_an_equal_measured_round_trip() -> None:
    """{0o1l}: the database refuses the claim, and the verdict is computed not assumed."""

    sql = _upgrade_sql()
    success_gate = _check_predicate(sql, "processing_runs", "run_succeeds_only_on_equal_hashes")
    evidence_gate = _check_predicate(
        sql, "processing_runs", "non_match_carries_difference_evidence"
    )

    # The two predicates are plain SQL, so they are executed rather than read.
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE processing_runs ("
            " status TEXT NOT NULL,"
            " integrity_outcome TEXT,"
            " source_sha256 TEXT,"
            " reconstruction_sha256 TEXT,"
            " first_difference_offset INTEGER,"
            " mismatch_span_id TEXT,"
            f" CHECK ({success_gate}),"
            f" CHECK ({evidence_gate}))"
        )

        def insert(**row: Any) -> None:
            values = {
                "status": "pending",
                "integrity_outcome": None,
                "source_sha256": None,
                "reconstruction_sha256": None,
                "first_difference_offset": None,
                "mismatch_span_id": None,
                **row,
            }
            connection.execute(
                "INSERT INTO processing_runs VALUES ("
                ":status, :integrity_outcome, :source_sha256, :reconstruction_sha256,"
                " :first_difference_offset, :mismatch_span_id)",
                values,
            )

        # Earned success: a match, both hashes present and equal.
        insert(
            status="succeeded",
            integrity_outcome="match",
            source_sha256=SOURCE_SHA256,
            reconstruction_sha256=SOURCE_SHA256,
        )

        # Every way of claiming success without that evidence is refused.
        for claimed in (
            {"status": "succeeded"},
            {"status": "succeeded", "integrity_outcome": "match"},
            {
                "status": "succeeded",
                "integrity_outcome": "match",
                "source_sha256": SOURCE_SHA256,
            },
            {
                "status": "succeeded",
                "integrity_outcome": "match",
                "source_sha256": SOURCE_SHA256,
                "reconstruction_sha256": "b" * 64,
            },
            {
                "status": "succeeded",
                "integrity_outcome": "mismatch",
                "source_sha256": SOURCE_SHA256,
                "reconstruction_sha256": SOURCE_SHA256,
                "first_difference_offset": 0,
            },
        ):
            with pytest.raises(sqlite3.IntegrityError):
                insert(**claimed)

        # A non-match must carry its difference evidence, not just a verdict.
        for unsupported in (
            {"status": "failed", "integrity_outcome": "mismatch"},
            {
                "status": "failed",
                "integrity_outcome": "length_boundary",
                "source_sha256": SOURCE_SHA256,
                "reconstruction_sha256": "b" * 64,
            },
        ):
            with pytest.raises(sqlite3.IntegrityError):
                insert(**unsupported)
        insert(
            status="failed",
            integrity_outcome="mismatch",
            source_sha256=SOURCE_SHA256,
            reconstruction_sha256="b" * 64,
            first_difference_offset=0,
        )
    finally:
        connection.close()

    # And the verdict the write path supplies is measured, with the offset that
    # supports it: a first differing byte, or the length boundary of a prefix.
    assert runtime_boundary._integrity_verdict(SOURCE_TEXT, SOURCE_TEXT) == ("match", None)
    assert runtime_boundary._integrity_verdict(SOURCE_TEXT, "First sentence. Second sentince.") == (
        "mismatch",
        len("First sentence. Second sent"),
    )
    assert runtime_boundary._integrity_verdict(SOURCE_TEXT, FIRST_SENTENCE) == (
        "length_boundary",
        len(FIRST_SENTENCE),
    )
    assert {"match", "mismatch", "length_boundary"} == set(INTEGRITY_OUTCOMES)


def test_a_non_matching_round_trip_is_recorded_truthfully_as_a_non_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{0o1l}: a lossy store yields a failed run carrying both hashes and the offset."""

    lossy = "First sentence. Second sentince."
    connection = _FakeConnection(reconstruction=lossy)
    engine = _install_engine(monkeypatch, connection)

    # Superseded by G-003/T-006/A-006: a non-matching round trip is now *refused*
    # rather than published with a failed run attached, so publication raises
    # ``IntegrityRefused`` instead of returning a reference dictionary. What this
    # test asserts about the recorded run is unchanged and still holds; only the
    # way the caller learns of it moved, and the refusal itself is proved in
    # ``test_integrity_refusal``.
    with pytest.raises(integrity_diagnostics.IntegrityRefused) as refusal:
        _publish(connection)

    run = connection.one("INSERT INTO processing_runs")
    assert run["status"] == "failed"
    assert run["integrity_outcome"] == "mismatch"
    assert run["source_sha256"] == SOURCE_SHA256
    assert run["reconstruction_sha256"] == hashlib.sha256(lossy.encode("utf-8")).hexdigest()
    assert run["reconstruction_sha256"] != run["source_sha256"]
    assert run["first_difference_offset"] == len("First sentence. Second sent")
    # Superseded by G-003/T-006/A-006: there is no reference dictionary to read
    # ``processing_run_status`` and ``integrity_outcome`` out of, because nothing
    # was published. The same two facts are asserted on the run row above, and
    # the caller receives them through the refusal instead.
    assert refusal.value.source_sha256 == run["source_sha256"]
    assert refusal.value.reconstruction_sha256 == run["reconstruction_sha256"]

    # Superseded twice. First by G-003/T-006/A-006, which made a mismatch a
    # refusal. Then by the fix for bug 1d359231: the hierarchy is now discarded
    # by a savepoint rather than by losing a whole transaction, so the evidence
    # is written by the same transaction that discarded it and the outcomes are
    # commit (the immutable original) and commit (the discard together with its
    # record). What the discard did is asserted through the first savepoint
    # below, which is where the rollback now lives; the second savepoint isolates
    # the optional diagnostic span.
    #
    # The state is still not appended on this path: a SourceState asserts a
    # version a reader could restore, and the refused version was discarded, so
    # asserting one would be a claim about rows that do not exist.
    assert engine.outcomes == ["commit", "commit"]
    assert [savepoint.outcome for savepoint in connection.savepoints] == [
        "rollback",
        "commit",
    ]
    assert connection.committed("INSERT INTO documents") == []
    assert connection.committed("INSERT INTO processing_runs") != []
    assert connection.issued("INSERT INTO source_states") == []
    assert connection.issued("INSERT INTO temporal_assertions") == []

    # A strict prefix is reported as a length boundary rather than a byte mismatch.
    truncated = _FakeConnection(reconstruction=FIRST_SENTENCE)
    _install_engine(monkeypatch, truncated)
    with pytest.raises(integrity_diagnostics.IntegrityRefused):
        _publish(truncated)
    boundary_run = truncated.one("INSERT INTO processing_runs")
    assert boundary_run["integrity_outcome"] == "length_boundary"
    assert boundary_run["first_difference_offset"] == len(FIRST_SENTENCE)
    assert boundary_run["status"] == "failed"


# ----------------------------------------------------------------------
# 3. The appended source state
# ----------------------------------------------------------------------


def test_source_state_is_appended_as_an_assertion_and_never_rewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{73a1}/{SO02}/{ik6b}: a restoration supersedes an accepted state, never edits it."""

    first = _FakeConnection()
    _install_engine(monkeypatch, first)
    _publish(first)

    assertion = first.one("INSERT INTO temporal_assertions")
    state = first.one("INSERT INTO source_states")
    assert assertion["payload_family"] == SOURCE_STATE_PAYLOAD_FAMILY
    assert assertion["payload_family"] == runtime_boundary.SOURCE_STATE_PAYLOAD_FAMILY
    assert assertion["object_id"] == DOCUMENT_ID
    assert assertion["assertion_kind"] == "payload"
    assert assertion["supersedes"] is None
    assert assertion["accepted_via"] == "document_create"
    assert assertion["accepted_by"] == RUN_ACTOR
    # The assertion UUID *is* the source-version identity the payload hangs from.
    assert state["assertion_id"] == assertion["assertion_id"]

    # The payload is lossless: a restoration command and selector, the separators
    # that make the stored chunks determine one text, the normalization profile
    # and its parameters, the hierarchy shape, and the hash to reproduce.
    manifest = json.loads(state["manifest"])
    assert manifest["restoration"]["command"] == "source_file_reconstruct"
    assert manifest["restoration"]["selector"] == {"document_id": str(DOCUMENT_ID)}
    assert manifest["restoration"]["separators"] == dict(
        runtime_boundary.RECONSTRUCTION_SEPARATORS
    )
    assert manifest["normalization"]["profile_id"] == (
        runtime_boundary.NORMALIZATION_PROFILE_ID
    )
    assert manifest["normalization"]["profile_version"] == (
        runtime_boundary.NORMALIZATION_PROFILE_VERSION
    )
    assert manifest["normalization"]["parameters"] == (
        runtime_boundary._normalization_parameters(_plan())
    )
    assert manifest["hierarchy"]["paragraph_count"] == 1
    assert manifest["hierarchy"]["chunk_count"] == 2
    assert manifest["hierarchy"]["chapter_ids"] == [
        str(first.one("INSERT INTO chapters")["id"])
    ]
    assert manifest["source"]["char_count"] == len(SOURCE_TEXT)
    assert state["restoration_sha256"] == SOURCE_SHA256
    assert state["recorded_encoding"] == "utf-8"
    assert state["comparison_scope"] == runtime_boundary.COMPARISON_SCOPE
    assert str(DOCUMENT_ID) in state["reconstruction_locator"]

    # The state and the run agree about which original they speak of, which is
    # exactly what the insert guard refuses to let fork.
    assert state["primary_source_id"] == first.one("INSERT INTO processing_runs")[
        "primary_source_id"
    ]
    assert state["produced_by_run_id"] == first.one("INSERT INTO processing_runs")["run_id"]

    # Republishing appends a successor naming the accepted state; it edits nothing.
    second = _FakeConnection(prior_assertion_id=PRIOR_ASSERTION_ID)
    _install_engine(monkeypatch, second)
    _publish(second, _plan(command="document_update"))
    successor = second.one("INSERT INTO temporal_assertions")
    assert successor["assertion_kind"] == "supersession"
    assert successor["supersedes"] == PRIOR_ASSERTION_ID
    assert successor["assertion_id"] != PRIOR_ASSERTION_ID
    assert successor["accepted_via"] == "document_update"
    for connection in (first, second):
        assert connection.issued("UPDATE source_states") == []
        assert connection.issued("DELETE FROM source_states") == []
        assert connection.issued("UPDATE temporal_assertions") == []
        # The registry row is locked first, so concurrent publications of one
        # document keep a monotonic revision order.
        registry, insert = connection.order_of(
            "FROM entity_uuid_registry", "INSERT INTO temporal_assertions"
        )
        assert registry < insert
        assert "COALESCE(MAX(revision_no), 0) + 1" in "".join(
            sql for sql, _ in connection.statements if "INSERT INTO temporal_assertions" in sql
        )


def test_the_migration_guards_source_state_family_lineage_and_append_only() -> None:
    """The payload table refuses a wrong family, a forked original, and any rewrite."""

    migration = _provenance_migration()
    sql = _upgrade_sql()

    insert_trigger = _squeeze(_statement(sql, f"CREATE TRIGGER {migration.STATE_GUARD_TRIGGER}"))
    assert insert_trigger == (
        f"CREATE TRIGGER {migration.STATE_GUARD_TRIGGER}"
        f" BEFORE INSERT ON {migration.STATE_TABLE}"
        f" FOR EACH ROW EXECUTE FUNCTION {migration.STATE_GUARD_FUNCTION}();"
    )
    guard = _squeeze(
        _statement(sql, f"CREATE OR REPLACE FUNCTION {migration.STATE_GUARD_FUNCTION}()")
    )
    assert f"IF asserted_family <> '{migration.PAYLOAD_FAMILY}' THEN" in guard
    assert migration.PAYLOAD_FAMILY == SOURCE_STATE_PAYLOAD_FAMILY
    assert "IF FOUND AND run_source_id IS DISTINCT FROM NEW.primary_source_id THEN" in guard
    assert "the evidence chain may not fork" in guard
    assert guard.count("ERRCODE = 'check_violation'") == 2
    # A missing assertion is left to the foreign key rather than masked here.
    assert "IF NOT FOUND THEN" in guard
    assert "RETURN NEW;" in guard

    update_trigger = _squeeze(
        _statement(sql, f"CREATE TRIGGER {migration.STATE_APPEND_ONLY_TRIGGER}")
    )
    assert update_trigger == (
        f"CREATE TRIGGER {migration.STATE_APPEND_ONLY_TRIGGER}"
        f" BEFORE UPDATE ON {migration.STATE_TABLE}"
        f" FOR EACH ROW EXECUTE FUNCTION {migration.STATE_APPEND_ONLY_FUNCTION}();"
    )
    append_only = _squeeze(
        _statement(sql, f"CREATE OR REPLACE FUNCTION {migration.STATE_APPEND_ONLY_FUNCTION}()")
    )
    # Every payload column is evidence, so every payload column is guarded.
    payload_columns = [column.name for column in SourceState.__table__.c]
    assert tuple(payload_columns) == migration.STATE_PAYLOAD_COLUMNS
    for column in payload_columns:
        assert f"NEW.{column} IS DISTINCT FROM OLD.{column}" in append_only
        assert f"offending_column := '{column}'" in append_only
    assert "ERRCODE = 'restrict_violation'" in append_only
    assert "supersedes_assertion_id names this one" in append_only

    # The run's audit identity is fixed at creation, and a terminal run is closed.
    run_guard = _squeeze(
        _statement(sql, f"CREATE OR REPLACE FUNCTION {migration.RUN_IMMUTABLE_FUNCTION}()")
    )
    assert f"IF OLD.status IN ({migration._TERMINAL_STATUS_SQL}) THEN" in run_guard
    assert set(migration.TERMINAL_RUN_STATUSES) <= set(PROCESSING_RUN_STATUSES)
    assert set(migration.TERMINAL_RUN_STATUSES) == {"succeeded", "failed", "aborted"}
    for column in migration.RUN_AUDIT_COLUMNS:
        assert f"NEW.{column} IS DISTINCT FROM OLD.{column}" in run_guard
    assert "audit identity is immutable" in run_guard
    assert f"UPDATE {migration.STATE_TABLE}" not in sql
    assert f"DELETE FROM {migration.STATE_TABLE}" not in sql


# ----------------------------------------------------------------------
# 4. Hierarchy lineage
# ----------------------------------------------------------------------


def test_every_produced_hierarchy_object_carries_its_state_and_run() -> None:
    """{5vcr}: provenance is resolvable from any single Document, Chapter or Paragraph."""

    assertion_id = UUID("73a10000-0000-4000-8000-0000000000aa")
    run_id = UUID("0d4e0000-0000-4000-8000-0000000000bb")
    upload_id = UUID("50c00000-0000-4000-8000-0000000000cc")

    aggregate = enrich_hierarchy(
        source_upload_id=upload_id,
        source_version="source-v1",
        chapters=[
            ChapterInput(
                paragraphs=(
                    ParagraphInput(
                        text=FIRST_SENTENCE, start=0, end=len(FIRST_SENTENCE), chunk_indexes=(0,)
                    ),
                    ParagraphInput(
                        text=SECOND_SENTENCE,
                        start=len(FIRST_SENTENCE) + 1,
                        end=len(SOURCE_TEXT),
                        chunk_indexes=(1,),
                    ),
                ),
                start=0,
                end=len(SOURCE_TEXT),
                heading="Chapter one",
            )
        ],
        semantic_chunks=_semantic_chunks(),
        source_state_assertion_id=assertion_id,
        processing_run_id=run_id,
    )

    trace = aggregate.traceability
    assert trace.source_state_assertion_id == assertion_id
    assert trace.processing_run_id == run_id

    produced = (
        (aggregate.document.id,)
        + tuple(chapter.id for chapter in aggregate.chapters)
        + tuple(paragraph.id for paragraph in aggregate.paragraphs)
        + tuple(UUID(str(chunk.uuid)) for chunk in aggregate.chunks)
    )
    assert len(aggregate.chapters) == 1
    assert len(aggregate.paragraphs) == 2
    assert set(trace.entity_provenance) == set(produced)
    for entity_id in produced:
        provenance = trace.entity_provenance[entity_id]
        assert provenance.source_state_assertion_id == assertion_id
        assert provenance.processing_run_id == run_id
        assert provenance.source_upload_id == upload_id
        assert provenance.source_version == "source-v1"
        assert trace.entity_source[entity_id] == (upload_id, "source-v1")

    # Half-stated evidence is refused whole: a state without its run resolves to
    # nothing, and a run that produced no state restores nothing.
    for half in ({"source_state_assertion_id": assertion_id}, {"processing_run_id": run_id}):
        with pytest.raises(HierarchyEnrichmentError, match="must be given together"):
            _minimal_hierarchy(upload_id, **half)
    with pytest.raises(HierarchyEnrichmentError, match="UUID version 4"):
        _minimal_hierarchy(
            upload_id,
            source_state_assertion_id=UUID(int=0, version=1),
            processing_run_id=run_id,
        )

    # Stated nowhere is still lawful: a hierarchy may be enriched before the
    # publishing execution has said anything, and then claims nothing.
    unstated = _minimal_hierarchy(upload_id)
    assert unstated.traceability.source_state_assertion_id is None
    assert unstated.traceability.processing_run_id is None
    assert all(
        provenance.processing_run_id is None
        for provenance in unstated.traceability.entity_provenance.values()
    )


def _semantic_chunks() -> list[Any]:
    """Two accepted public chunks, built the way the hierarchy boundary expects."""

    document = Document(id=UUID("50c00000-0000-4000-8000-000000000001"))
    chapter = Chapter(document=document, id=UUID("50c00000-0000-4000-8000-000000000002"))
    paragraph = Paragraph(
        chapter=chapter, id=UUID("50c00000-0000-4000-8000-000000000003"), text=SOURCE_TEXT
    )
    return list(
        build_semantic_chunks(
            document,
            chapter,
            paragraph,
            [
                ChunkSpec(body=FIRST_SENTENCE, start=0, end=len(FIRST_SENTENCE), ordinal=0),
                ChunkSpec(
                    body=SECOND_SENTENCE,
                    start=len(FIRST_SENTENCE) + 1,
                    end=len(SOURCE_TEXT),
                    ordinal=1,
                ),
            ],
        )
    )


def _minimal_hierarchy(upload_id: UUID, **provenance: Any) -> Any:
    chunks = _semantic_chunks()
    return enrich_hierarchy(
        source_upload_id=upload_id,
        source_version="source-v1",
        chapters=[
            ChapterInput(
                paragraphs=(
                    ParagraphInput(
                        text=SOURCE_TEXT, start=0, end=len(SOURCE_TEXT), chunk_indexes=(0, 1)
                    ),
                ),
                start=0,
                end=len(SOURCE_TEXT),
            )
        ],
        semantic_chunks=chunks,
        **provenance,
    )


# ----------------------------------------------------------------------
# 5. Idempotency and rollback
# ----------------------------------------------------------------------


def test_an_idempotent_replay_answers_from_storage_without_minting_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{xmpp}/{IN01}: a replay writes nothing -- no original, no run, no state."""

    quiescent = {
        "document_needs_revectorize": False,
        "document_body_sha256": SOURCE_SHA256,
        "file_id": FILE_ID,
        "file_content_sha256": ORIGINAL_SHA256,
        "file_body_sha256": SOURCE_SHA256,
        "file_needs_revectorize": False,
        "file_needs_rechunk": False,
    }
    connection = _FakeConnection(
        document_state=quiescent,
        committed_document={
            "source_version_id": "source-v1",
            "chunking_strategy": "paragraph",
        },
        existing_chunk_ids=(FIRST_SENTENCE_ID, SECOND_SENTENCE_ID),
    )
    _install_engine(monkeypatch, connection)
    repository = _repository()

    reference = asyncio.run(repository.find_committed_version(DOCUMENT_ID, "source-v1"))

    assert reference is not None
    assert reference["idempotent"] is True
    assert reference["idempotent_reason"] == "source_version_match"
    assert reference["document_id"] == str(DOCUMENT_ID)
    assert reference["chunk_ids"] == (str(FIRST_SENTENCE_ID), str(SECOND_SENTENCE_ID))
    # Deciding "nothing to do" must not be able to leave anything behind.
    assert all(sql.startswith("SELECT") for sql, _ in connection.statements)
    for table in ("primary_sources", "processing_runs", "source_states", "temporal_assertions"):
        assert connection.issued(f"INSERT INTO {table}") == []

    # And the orchestrator stops there: nothing is mapped and nothing published.
    class _Mapper:
        def __init__(self) -> None:
            self.calls = 0

        def map(self, aggregate: Any) -> PersistencePlan:
            self.calls += 1
            return _plan()

    mapper = _Mapper()
    outcome = asyncio.run(
        publish_document(_minimal_hierarchy(DOCUMENT_ID), mapper, repository)
    )
    assert isinstance(outcome, IdempotentReplay)
    assert outcome.status == "idempotent_replay"
    assert outcome.references["idempotent_reason"] == "source_version_match"
    assert mapper.calls == 0
    assert connection.issued("INSERT INTO processing_runs") == []


def test_a_failed_publication_rolls_back_the_hierarchy_and_its_provenance_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{xmpp}: one transaction, so a hierarchy without its run is unrepresentable."""

    # A failure while writing the hierarchy: the provenance rows are never even
    # reached, and the block rolls back rather than commits.
    early = _FakeConnection(fail_on="INSERT INTO paragraphs")
    engine = _install_engine(monkeypatch, early)
    with pytest.raises(RuntimeError, match="storage refused"):
        _publish(early)
    # Superseded by G-003/T-006/A-006: the evidence prologue commits the
    # immutable original before the hierarchy block is opened, so the failing
    # publication now shows that commit ahead of its rollback. The claim is
    # unchanged --- the block that writes the hierarchy and its provenance rolls
    # back as one --- and the original is not part of what rolls back, because
    # the bytes were genuinely received whether or not anything is published.
    assert engine.blocks == 2
    assert engine.outcomes == ["commit", "rollback"]
    assert engine.disposed == 1
    for table in ("processing_runs", "source_states", "temporal_assertions"):
        assert early.issued(f"INSERT INTO {table}") == []

    # A failure after the hierarchy is written: the same single block rolls the
    # whole publication back, so no partial hierarchy stays visible either.
    late = _FakeConnection(registered_document=None)
    late_engine = _install_engine(monkeypatch, late)
    with pytest.raises(ValueError, match="not a registered object"):
        _publish(late)
    # Superseded for the same reason: prologue commit, then the hierarchy block
    # rolls the whole publication back.
    assert late_engine.blocks == 2
    assert late_engine.outcomes == ["commit", "rollback"]
    assert late.issued("INSERT INTO documents") != []
    assert late.issued("INSERT INTO source_states") == []

    # The orchestrator reports that as one typed non-publication with no
    # canonical version reference at all.
    repository = _repository()

    class _Mapper:
        def map(self, aggregate: Any) -> PersistencePlan:
            return _plan()

    rolled_back = _FakeConnection(registered_document=None)
    _install_engine(monkeypatch, rolled_back)
    outcome = asyncio.run(
        publish_document(_minimal_hierarchy(DOCUMENT_ID), _Mapper(), repository)
    )
    assert isinstance(outcome, RolledBackPublication)
    assert outcome.status == "rolled_back"
    assert outcome.references is None
    assert outcome.canonical_version_refs is None
    assert outcome.failure.stage == "repository_transaction"
    assert outcome.failure.error_type == "ValueError"


# ----------------------------------------------------------------------
# 6. The mapping is the migrated schema
# ----------------------------------------------------------------------


def test_orm_declarations_are_the_migrated_provenance_tables() -> None:
    """Same columns, same constraints, same names -- on both sides."""

    migration = _provenance_migration()
    sql = _upgrade_sql()

    for declaration, table_name in (
        (PrimarySource, migration.SOURCE_TABLE),
        (ProcessingRun, migration.RUN_TABLE),
        (SourceState, migration.STATE_TABLE),
    ):
        table = declaration.__table__
        assert table.name == table_name
        assert metadata.tables[table_name] is table
        emitted = _squeeze(_statement(sql, f"CREATE TABLE {table_name}"))
        mapped = _squeeze(str(CreateTable(table).compile(dialect=postgresql.dialect())))
        assert emitted == f"{mapped};"

    # An immutable row carries no lifecycle triple and no revision counter: a
    # correction is a new row, never an edit of this one.
    lifecycle = {"updated_at", "is_deleted", "deleted_at", "revision_no"}
    assert lifecycle.isdisjoint(set(PrimarySource.__table__.c.keys()))
    assert lifecycle.isdisjoint(set(ProcessingRun.__table__.c.keys()))

    # The state payload owns no second version system: every temporal property
    # belongs to the assertion it hangs from.
    assert [column.name for column in SourceState.__table__.primary_key.columns] == [
        "assertion_id"
    ]
    forbidden = {
        "id",
        "revision_no",
        "effective_from",
        "effective_until",
        "recorded_at",
        "is_current",
        "supersedes_assertion_id",
    }
    assert forbidden.isdisjoint(set(SourceState.__table__.c.keys()))

    # Deletion of an original or a run is refused while evidence still cites it.
    restrict = {
        (constraint.table.name, tuple(element.target_fullname for element in constraint.elements))
        for declaration in (ProcessingRun, SourceState)
        for constraint in declaration.__table__.foreign_key_constraints
        if constraint.ondelete == "RESTRICT"
    }
    assert ("processing_runs", ("primary_sources.id",)) in restrict
    assert ("source_states", ("primary_sources.id",)) in restrict
    assert ("source_states", ("processing_runs.run_id",)) in restrict

    # Losing an output must not erase the record that the run happened.
    outputs = {
        constraint.elements[0].target_fullname: constraint.ondelete
        for constraint in ProcessingRun.__table__.foreign_key_constraints
        if constraint.elements[0].target_fullname
        in {"documents.id", "chapters.id", "semantic_chunks.id"}
    }
    assert outputs == {
        "documents.id": "SET NULL",
        "chapters.id": "SET NULL",
        "semantic_chunks.id": "SET NULL",
    }

    # Registry enrolment is deliberately absent, so no trigger claims these kinds.
    assert "primary_source" not in REGISTRY_KINDS
    assert "processing_run" not in REGISTRY_KINDS
    for table_name in (migration.SOURCE_TABLE, migration.RUN_TABLE, migration.STATE_TABLE):
        assert f"trg_{table_name}_register_entity_uuid" not in sql

    # And the downgrade takes the association apart before the table it points at.
    down = _offline_sql(_provenance_migration(), "downgrade")
    assert down.index(f"DROP CONSTRAINT fk_{migration.FILE_TABLE}") < down.index(
        f"DROP TABLE {migration.SOURCE_TABLE}"
    )
    for table_name in (migration.SOURCE_TABLE, migration.RUN_TABLE, migration.STATE_TABLE):
        assert f"DROP TABLE {table_name}" in down
