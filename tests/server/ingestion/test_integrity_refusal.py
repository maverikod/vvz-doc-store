"""The refusal, the evidence that outlives it, and the untouched matching path.

G-003/T-006 makes a non-matching round trip a *refusal* rather than a recorded
disappointment. ``{0o1l}`` says ingestion is not successful when the normalized
source and the scope reconstructed from it differ, and ``{xmpp}`` says a
publication is atomic; together they say the hierarchy must not become visible
while the record that it was refused must survive. ``PublicationRepository._publish``
therefore runs three transactions, and *which of them commits* is the whole of
the semantics this module proves:

1. the evidence prologue commits the immutable ``PrimarySource`` alone, so the
   ``NOT NULL``/``RESTRICT`` parent of a failed run exists after a rollback;
2. the hierarchy block writes everything the publication would make visible and
   measures the round trip inside itself --- on a match it also writes the run
   and appends the ``SourceState`` and commits, on a non-match it commits
   nothing;
3. the refusal-evidence block runs only after such a rollback and commits the
   failed ``ProcessingRun`` and its ``SourceSpan``, then the refusal is raised.

The technique is the one ``test_source_provenance`` established and is imported
from it rather than re-invented: the same recording ``_FakeConnection``, which
answers the reconstruction query from the chunk payloads the write path just
inserted, and the same production repository with only its engine replaced. The
one addition is an engine that attributes each recorded statement to the
``engine.begin()`` block that issued it, because "the block that wrote the
hierarchy rolled back and a *later* block committed the failed run" is a claim
about blocks, and an engine that only counts them cannot state it.

Three assertions in ``test_source_provenance`` were superseded by this step and
say so at their site: that a mismatching publication returns a reference
dictionary and commits (it now raises ``IntegrityRefused`` and its hierarchy
block rolls back), and the two ``engine.blocks == 1`` counts, which predate the
evidence prologue. Every substantive claim those tests made is still made there,
and the behaviour that replaced the stale part is proved here.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest

from doc_store_server.ingestion import (
    integrity_diagnostics,
    publication_repository,
    runtime_boundary,
)
from doc_store_server.ingestion.persistence_plan import PersistencePlan
from doc_store_server.ingestion.publication import (
    IntegrityRefusedPublication,
    RolledBackPublication,
    publish_document,
)
from doc_store_server.ingestion.publication_repository import PublicationRepository
from doc_store_server.ingestion.runtime_boundary import (
    InMemoryRuntimeStatus,
    RuntimeIngestionBoundary,
)
from doc_store_server.ingestion.svo_chunking import RuntimeChunk

from test_source_provenance import (
    DATABASE_URL,
    DOCUMENT_ID,
    FIRST_SENTENCE,
    FIRST_SENTENCE_ID,
    OPERATION_ID,
    PARAGRAPH_CHUNK_ID,
    SECOND_SENTENCE_ID,
    SOURCE_SHA256,
    SOURCE_TEXT,
    _FakeChunker,
    _FakeConnection,
    _minimal_hierarchy,
    _plan,
    _repository,
)


# The lossy store the fake connection simulates: the whole compared scope comes
# back as one payload that differs from the source in a single character.
LOSSY_TEXT = "First sentence. Second sentince."
LOSSY_SHA256 = hashlib.sha256(LOSSY_TEXT.encode("utf-8")).hexdigest()
MISMATCH_OFFSET = len("First sentence. Second sent")

# A longer source whose difference is far enough from both ends that the
# disclosure window is genuinely bounded rather than incidentally short, and
# which carries a digit run the redaction policy has to remove.
LONG_FIRST = "The remittance advice was archived under reference 4815162342."
LONG_SECOND = "Totals must agree before anyone signs."
LONG_SOURCE = f"{LONG_FIRST} {LONG_SECOND}"
LONG_LOSSY = LONG_SOURCE.replace("agree", "agrea")
ACCOUNT_DIGITS = "4815162342"


# ----------------------------------------------------------------------
# The one addition to the imported technique: statements attributed to blocks
# ----------------------------------------------------------------------


@dataclass
class _Block:
    """One ``engine.begin()`` block: what it issued, and how it ended."""

    index: int
    statements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    outcome: str | None = None

    def issued(self, fragment: str) -> list[dict[str, Any]]:
        return [params for sql, params in self.statements if fragment in sql]

    def one(self, fragment: str) -> dict[str, Any]:
        matches = self.issued(fragment)
        assert len(matches) == 1, f"expected exactly one {fragment!r} in block {self.index}"
        return matches[0]


class _BlockRecordingTransaction:
    def __init__(self, engine: "_BlockRecordingEngine") -> None:
        self._engine = engine
        self._start = 0
        self._block = _Block(index=-1)

    def __enter__(self) -> _FakeConnection:
        engine = self._engine
        engine.blocks += 1
        self._start = len(engine.connection.statements)
        self._block = _Block(index=len(engine.log))
        engine.log.append(self._block)
        return engine.connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        engine = self._engine
        self._block.statements = list(engine.connection.statements[self._start :])
        self._block.outcome = "rollback" if exc_type is not None else "commit"
        engine.outcomes.append(self._block.outcome)
        return False


class _BlockRecordingEngine:
    """``_FakeEngine`` plus the statements each block issued before it ended.

    The connection, and therefore every answer the write path reads back, is the
    imported ``_FakeConnection``; only the attribution of statements to blocks is
    new, because that is what this step's central claim is about.
    """

    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.blocks = 0
        self.outcomes: list[str] = []
        self.disposed = 0
        self.log: list[_Block] = []

    def begin(self) -> _BlockRecordingTransaction:
        return _BlockRecordingTransaction(self)

    def connect(self) -> _BlockRecordingTransaction:
        return _BlockRecordingTransaction(self)

    def dispose(self) -> None:
        self.disposed += 1

    def block_with(self, fragment: str) -> _Block:
        """The single block that issued a statement containing ``fragment``."""

        matches = [block for block in self.log if block.issued(fragment)]
        assert len(matches) == 1, (
            f"expected exactly one block issuing {fragment!r}, got {len(matches)}"
        )
        return matches[0]


def _install_engine(
    monkeypatch: pytest.MonkeyPatch, connection: _FakeConnection
) -> _BlockRecordingEngine:
    engine = _BlockRecordingEngine(connection)
    monkeypatch.setattr(PublicationRepository, "_engine", lambda _self: engine)
    monkeypatch.setenv(runtime_boundary.RUN_ACTOR_ENV, "doc-store:tests:integrity-refusal")
    return engine


def _publish(
    connection: _FakeConnection,
    plan: PersistencePlan | None = None,
    units: tuple[tuple[RuntimeChunk, ...], tuple[RuntimeChunk, ...]] | None = None,
) -> dict[str, Any]:
    """Run the production publication over the recording connection."""

    paragraph_chunks, sentence_chunks = units or _default_units()
    return asyncio.run(
        _repository().publish_transaction(plan or _plan(), paragraph_chunks, sentence_chunks)
    )


def _default_units() -> tuple[tuple[RuntimeChunk, ...], tuple[RuntimeChunk, ...]]:
    return _units(SOURCE_TEXT, (FIRST_SENTENCE, SOURCE_TEXT[len(FIRST_SENTENCE) + 1 :]))


def _units(
    paragraph_text: str, sentences: tuple[str, ...]
) -> tuple[tuple[RuntimeChunk, ...], tuple[RuntimeChunk, ...]]:
    """One paragraph and its sentences, as the chunker would have produced them."""

    paragraph = RuntimeChunk(
        uuid=PARAGRAPH_CHUNK_ID,
        text=paragraph_text,
        start=0,
        end=len(paragraph_text),
        ordinal=0,
        metadata={},
    )
    identifiers = (FIRST_SENTENCE_ID, SECOND_SENTENCE_ID)
    chunks = []
    cursor = 0
    for ordinal, (identifier, sentence) in enumerate(zip(identifiers, sentences)):
        start = paragraph_text.index(sentence, cursor)
        chunks.append(
            RuntimeChunk(
                uuid=identifier,
                text=sentence,
                start=start,
                end=start + len(sentence),
                ordinal=ordinal,
                metadata={},
            )
        )
        cursor = start + len(sentence)
    return (paragraph,), tuple(chunks)


def _long_source_case() -> tuple[
    PersistencePlan, tuple[tuple[RuntimeChunk, ...], tuple[RuntimeChunk, ...]], int
]:
    """The long-source plan, its chunk units, and the byte offset that differs."""

    plan = _plan(
        text_value=LONG_SOURCE,
        length=len(LONG_SOURCE),
        body_sha256=hashlib.sha256(LONG_SOURCE.encode("utf-8")).hexdigest(),
        byte_length=len(LONG_SOURCE.encode("utf-8")),
    )
    offset = next(
        index
        for index, (source_byte, lossy_byte) in enumerate(
            zip(LONG_SOURCE.encode("utf-8"), LONG_LOSSY.encode("utf-8"))
        )
        if source_byte != lossy_byte
    )
    return plan, _units(LONG_SOURCE, (LONG_FIRST, LONG_SECOND)), offset


class _Mapper:
    """The mapper seam ``publish_document`` calls once, with a fixed plan."""

    def map(self, aggregate: Any) -> PersistencePlan:
        return _plan()


# ----------------------------------------------------------------------
# 1. The hierarchy rolls back; the failed run persists in a later block
# ----------------------------------------------------------------------


def test_a_non_matching_round_trip_rolls_the_hierarchy_back_and_commits_the_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{0o1l}/{xmpp}: nothing publishable commits, and the refusal is on the record."""

    connection = _FakeConnection(reconstruction=LOSSY_TEXT)
    engine = _install_engine(monkeypatch, connection)

    with pytest.raises(integrity_diagnostics.IntegrityRefused) as refusal:
        _publish(connection)

    # Superseded by the fix for bug 1d359231. The refusal used to be a third
    # transaction opened after the hierarchy transaction was lost, which is
    # exactly what made the evidence losable. The hierarchy is now discarded by
    # a savepoint inside the transaction that records the refusal, so there are
    # two blocks and both commit -- and the discard is asserted where it now
    # lives, on the savepoint.
    assert engine.blocks == 2
    assert engine.outcomes == ["commit", "commit"]
    assert engine.disposed == 1

    # The whole hierarchy shares one savepoint, and that savepoint rolled back.
    assert [savepoint.outcome for savepoint in connection.savepoints] == ["rollback"]
    for statement in (
        "INSERT INTO documents",
        "INSERT INTO chapters",
        "INSERT INTO paragraphs",
        "INSERT INTO semantic_chunks ",
        "INSERT INTO semantic_chunk_texts ",
        "INSERT INTO files",
    ):
        assert connection.issued(statement), f"{statement!r} was never issued"
        assert connection.committed(statement) == [], (
            f"{statement!r} survived a savepoint that rolled back"
        )

    # The failed run is not inside the discarded savepoint, so it persists --
    # and it persists by the same commit that discarded the hierarchy, which is
    # the whole point: the two can no longer disagree.
    assert connection.committed("INSERT INTO processing_runs") != []
    assert connection.committed("INSERT INTO source_spans") != []
    evidence = engine.block_with("INSERT INTO processing_runs")
    assert evidence.outcome == "commit"
    run = evidence.one("INSERT INTO processing_runs")
    assert run["status"] == "failed"
    assert run["integrity_outcome"] == "mismatch"

    # The refused version left nothing behind that would make it look published:
    # no state, no assertion, and no second, contradicting run.
    for table in ("source_states", "temporal_assertions"):
        assert connection.issued(f"INSERT INTO {table}") == []
    assert len(connection.issued("INSERT INTO processing_runs")) == 1

    # And the caller is told, once, in the one shape a refusal has.
    assert refusal.value.source_sha256 == SOURCE_SHA256
    assert refusal.value.reconstruction_sha256 == LOSSY_SHA256
    assert refusal.value.controlled_error["code"] == "SOURCE_INTEGRITY_MISMATCH"


# ----------------------------------------------------------------------
# 2. The immutable original survives the rollback independently
# ----------------------------------------------------------------------


def test_the_immutable_original_is_committed_before_the_hierarchy_that_may_roll_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{5434}: the failed run's RESTRICT parent exists whatever the hierarchy does."""

    connection = _FakeConnection(reconstruction=LOSSY_TEXT)
    engine = _install_engine(monkeypatch, connection)

    with pytest.raises(integrity_diagnostics.IntegrityRefused):
        _publish(connection)

    prologue = engine.block_with("INSERT INTO primary_sources")
    assert prologue.index == 0
    assert prologue.outcome == "commit"
    # The prologue is exactly the original and nothing else: it must not be able
    # to make any part of the refused publication durable.
    written = {sql.split(" (")[0] for sql, _ in prologue.statements if sql.startswith("INSERT")}
    assert written == {"INSERT INTO primary_sources"}
    assert prologue.index < engine.block_with("INSERT INTO documents").index

    original = prologue.one("INSERT INTO primary_sources")
    run = engine.block_with("INSERT INTO processing_runs").one("INSERT INTO processing_runs")
    # NOT NULL under ON DELETE RESTRICT: the failed run names a row that is
    # committed, so the evidence transaction has a valid parent to point at.
    assert run["primary_source_id"] == original["id"]
    assert run["primary_source_id"] is not None
    assert original["original_sha256"] == _plan().content_sha256


# ----------------------------------------------------------------------
# 3. What the run and the span carry
# ----------------------------------------------------------------------


def test_the_refused_run_and_its_span_carry_what_the_requirement_enumerates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{0o1l}: both hashes, the locator, the span, the policy, the identifiers."""

    connection = _FakeConnection(reconstruction=LOSSY_TEXT)
    engine = _install_engine(monkeypatch, connection)

    with pytest.raises(integrity_diagnostics.IntegrityRefused) as refusal:
        _publish(connection)

    evidence = engine.block_with("INSERT INTO processing_runs")
    run = evidence.one("INSERT INTO processing_runs")
    span = evidence.one("INSERT INTO source_spans")

    # The run is the audit record of a refusal, so it states the verdict and the
    # measurement that supports it.
    assert run["status"] == "failed"
    assert run["integrity_outcome"] == "mismatch"
    assert run["source_sha256"] == SOURCE_SHA256
    assert run["reconstruction_sha256"] == LOSSY_SHA256
    assert run["reconstruction_sha256"] != run["source_sha256"]
    assert run["first_difference_offset"] == MISMATCH_OFFSET

    # It cites the span, and names the disclosure decision that produced it.
    assert run["mismatch_span_id"] is not None
    assert run["mismatch_span_id"] == span["span_id"]
    assert run["diagnostic_authorization"] == publication_repository.DIAGNOSTIC_AUTHORIZATION
    assert run["redaction_policy"] == publication_repository.DIAGNOSTIC_REDACTION_POLICY
    # Pinned rather than merely referenced: changing what ingestion discloses, or
    # to whom, is a deliberate policy edit and has to fail here first.
    assert publication_repository.DIAGNOSTIC_AUTHORIZATION == "owner"
    assert publication_repository.DIAGNOSTIC_REDACTION_POLICY == "redact_pii"
    assert (
        publication_repository.DIAGNOSTIC_AUTHORIZATION
        in integrity_diagnostics.CONTEXT_AUTHORIZED_DECISIONS
    )
    assert (
        publication_repository.DIAGNOSTIC_REDACTION_POLICY
        in integrity_diagnostics.REDACTION_POLICIES
    )

    # The run cites no hierarchy row, because migration 0019 declares all three
    # as SET NULL foreign keys into rows this refusal has just rolled back.
    assert run["document_id"] is None
    assert run["chapter_id"] is None
    assert run["chunk_id"] is None

    # The identifiers {0o1l} demands therefore ride on the span, whose columns
    # are unconstrained UUIDs and so survive the rollback.
    chapter_id = connection.one("INSERT INTO chapters")["id"]
    assert span["document_id"] == DOCUMENT_ID
    assert span["chapter_id"] == chapter_id
    assert span["chunk_id"] == SECOND_SENTENCE_ID
    assert span["produced_by_run_id"] == run["run_id"]

    # Exactly one difference locator, and the bounded window it digested.
    assert span["byte_offset"] == MISMATCH_OFFSET
    assert span["length_boundary"] is None
    assert span["character_start"] == 0
    assert span["character_end"] == len(SOURCE_TEXT)
    assert span["fragment_sha256"] == hashlib.sha256(SOURCE_TEXT.encode("utf-8")).hexdigest()
    assert span["context_before"] == SOURCE_TEXT[:MISMATCH_OFFSET]
    assert span["context_after"] == SOURCE_TEXT[MISMATCH_OFFSET:]
    assert span["authorization_decision"] == publication_repository.DIAGNOSTIC_AUTHORIZATION
    assert span["redaction_policy"] == publication_repository.DIAGNOSTIC_REDACTION_POLICY

    # The run exists before the span that names it: source_spans.produced_by_run_id
    # is RESTRICT, so the other order would not be insertable.
    order = [
        index
        for index, (sql, _) in enumerate(evidence.statements)
        if sql.startswith("INSERT INTO processing_runs") or sql.startswith("INSERT INTO source_spans")
    ]
    assert order == sorted(order)
    assert evidence.statements[order[0]][0].startswith("INSERT INTO processing_runs")

    # The same values reach the caller, rendered for transport.
    context = refusal.value.controlled_error["context"]
    assert context["span_id"] == str(span["span_id"])
    assert context["document_id"] == str(DOCUMENT_ID)
    assert context["chapter_id"] == str(chapter_id)
    assert context["chunk_id"] == str(SECOND_SENTENCE_ID)


def test_the_disclosed_context_is_bounded_and_redacted_before_it_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{0o1l}: policy-bounded and redacted context, not a slice of the source."""

    plan, units, offset = _long_source_case()
    connection = _FakeConnection(reconstruction=LONG_LOSSY)
    engine = _install_engine(monkeypatch, connection)

    with pytest.raises(integrity_diagnostics.IntegrityRefused):
        _publish(connection, plan, units)

    span = engine.block_with("INSERT INTO source_spans").one("INSERT INTO source_spans")
    limit = runtime_boundary.DIAGNOSTIC_CONTEXT_LIMIT
    assert limit == 64
    exact_before = LONG_SOURCE[offset - limit : offset]
    exact_after = LONG_SOURCE[offset : offset + limit]

    # Bounded: each side is at most the limit, whatever the size of the source.
    assert len(exact_before) == limit
    assert len(span["context_before"]) == limit
    assert len(span["context_after"]) <= limit
    assert span["character_start"] == offset - limit
    assert span["character_end"] == len(LONG_SOURCE)

    # Redacted: the account-like digit run inside the window never reaches storage,
    # and the redaction is exactly what the named policy does, masked in place.
    assert ACCOUNT_DIGITS in exact_before
    assert ACCOUNT_DIGITS not in span["context_before"]
    assert "*" * len(ACCOUNT_DIGITS) in span["context_before"]
    assert span["context_before"] == integrity_diagnostics.REDACTION_POLICIES["redact_pii"](
        exact_before
    )
    assert span["context_after"] == exact_after
    assert span["redaction_applied"] is True

    # The digest covers the exact window, never its redacted rendering, so two
    # refusals over one source correlate under different policies.
    assert span["fragment_sha256"] == hashlib.sha256(
        LONG_SOURCE[offset - limit : len(LONG_SOURCE)].encode("utf-8")
    ).hexdigest()
    assert span["byte_offset"] == offset


# ----------------------------------------------------------------------
# 4. A strict prefix is a length boundary, not a byte mismatch
# ----------------------------------------------------------------------


def test_a_strict_prefix_is_refused_as_a_length_boundary_rather_than_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{0o1l}: the locator says which kind of difference was found."""

    connection = _FakeConnection(reconstruction=FIRST_SENTENCE)
    engine = _install_engine(monkeypatch, connection)

    with pytest.raises(integrity_diagnostics.IntegrityRefused) as refusal:
        _publish(connection)

    # Superseded with the savepoint topology: two blocks, both committing, and
    # the hierarchy discarded by the savepoint inside the second.
    assert engine.outcomes == ["commit", "commit"]
    assert [savepoint.outcome for savepoint in connection.savepoints] == ["rollback"]
    assert connection.committed("INSERT INTO documents") == []

    evidence = engine.block_with("INSERT INTO processing_runs")
    run = evidence.one("INSERT INTO processing_runs")
    span = evidence.one("INSERT INTO source_spans")
    assert run["status"] == "failed"
    assert run["integrity_outcome"] == "length_boundary"
    assert run["first_difference_offset"] == len(FIRST_SENTENCE)
    assert run["reconstruction_sha256"] == hashlib.sha256(
        FIRST_SENTENCE.encode("utf-8")
    ).hexdigest()

    # Exactly one locator, and it is the boundary one.
    assert span["length_boundary"] == len(FIRST_SENTENCE)
    assert span["byte_offset"] is None
    context = refusal.value.controlled_error["context"]
    assert context["length_boundary"] == len(FIRST_SENTENCE)
    assert context["first_difference_offset"] is None
    assert "strict prefix" in refusal.value.controlled_error["message"]
    # The boundary is attributed to where the reconstruction stopped, not to
    # nothing: a refusal that named no chunk would locate the loss nowhere.
    assert span["chunk_id"] == SECOND_SENTENCE_ID


# ----------------------------------------------------------------------
# 5. The caller cannot mistake a refusal for a successful ingestion
# ----------------------------------------------------------------------


def test_the_ingestion_boundary_answers_a_refusal_as_a_rolled_back_non_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{0o1l}: status rolled_back, the controlled failure, and no references at all."""

    connection = _FakeConnection(reconstruction=LOSSY_TEXT)
    engine = _install_engine(monkeypatch, connection)
    status = InMemoryRuntimeStatus()
    boundary = RuntimeIngestionBoundary(DATABASE_URL, status, _FakeChunker())

    result = asyncio.run(
        boundary(
            document_id=str(DOCUMENT_ID),
            source_version_id="source-v1",
            operation_id=OPERATION_ID,
            command="document_create",
            raw_text=SOURCE_TEXT,
            chunking_strategy="paragraph",
        )
    )

    assert result["status"] == "rolled_back"
    failure = result["failure"]
    assert failure["code"] == integrity_diagnostics.INTEGRITY_MISMATCH_CODE
    assert failure["code"] == "SOURCE_INTEGRITY_MISMATCH"

    run = engine.block_with("INSERT INTO processing_runs").one("INSERT INTO processing_runs")
    span = engine.block_with("INSERT INTO source_spans").one("INSERT INTO source_spans")
    context = failure["context"]
    assert context["source_sha256"] == run["source_sha256"]
    assert context["reconstruction_sha256"] == run["reconstruction_sha256"] == LOSSY_SHA256
    assert context["first_difference_offset"] == run["first_difference_offset"] is not None
    assert context["length_boundary"] is None
    assert context["context_before"] == span["context_before"]
    assert context["context_after"] == span["context_after"]
    assert context["redaction_policy"] == publication_repository.DIAGNOSTIC_REDACTION_POLICY
    assert context["document_id"] == str(DOCUMENT_ID)
    assert context["chapter_id"] == str(connection.one("INSERT INTO chapters")["id"])
    assert context["chunk_id"] == str(SECOND_SENTENCE_ID)

    # It is NOT the success-shaped reference dictionary: the mapping a committed
    # publication returns carries the canonical version, and this one has none of
    # it, so no caller keyed on those names can read a refusal as a success.
    assert set(result) == {"status", "failure"}
    for key in (
        "document_id",
        "source_version_id",
        "source_version",
        "chapter_ids",
        "paragraph_ids",
        "chunk_ids",
        "primary_source_id",
        "processing_run_id",
        "processing_run_status",
        "integrity_outcome",
        "source_state_assertion_id",
        "idempotent",
    ):
        assert key not in result
    snapshot = status.get_status(OPERATION_ID)
    assert snapshot["status"] == "failed"
    assert snapshot["version_reference"] is None
    assert snapshot["document_reference"] is None
    assert snapshot["failure"]["code"] == "SOURCE_INTEGRITY_MISMATCH"


# ----------------------------------------------------------------------
# 6. The orchestrator's own typed outcome
# ----------------------------------------------------------------------


def test_publish_document_answers_a_refusal_with_its_own_typed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal is a verdict, not an incidental failure, and is typed as one."""

    connection = _FakeConnection(reconstruction=LOSSY_TEXT)
    _install_engine(monkeypatch, connection)

    outcome = asyncio.run(
        publish_document(_minimal_hierarchy(DOCUMENT_ID), _Mapper(), _repository())
    )

    assert isinstance(outcome, IntegrityRefusedPublication)
    assert not isinstance(outcome, RolledBackPublication)
    assert outcome.status == "integrity_refused"
    assert outcome.references is None
    assert outcome.canonical_version_refs is None
    assert outcome.source_sha256 == SOURCE_SHA256
    assert outcome.reconstruction_sha256 == LOSSY_SHA256
    assert outcome.diagnostic.document_id == DOCUMENT_ID
    assert outcome.diagnostic.byte_offset == MISMATCH_OFFSET
    # The refusal keeps its payload where a RolledBackPublication would have had
    # only a stage string, which is exactly why the two are separate types.
    assert not hasattr(outcome, "failure")


def test_a_failure_inside_the_refusal_evidence_transaction_is_an_ordinary_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1d359231, closed: a refused publication with no record is unreachable.

    This case used to have three possible endings, and the bad one was reachable:
    the hierarchy transaction was already lost by the time the evidence
    transaction ran, so a storage error there left a measured mismatch with
    nothing on the record and a caller who could not tell it from a crash.

    With the hierarchy discarded by a savepoint, the discard and the record are
    the same transaction. A failure while writing the evidence therefore takes
    the whole transaction with it: nothing is published *and* nothing is
    recorded, which is the consistent reading -- the publication simply did not
    happen. The caller gets an ordinary storage failure, which is honest,
    because no refusal was ever committed.

    What is gone is the third ending. There is no longer any interleaving in
    which the hierarchy is discarded and its evidence is not, so a caller
    holding an ``IntegrityRefusedPublication`` can rely on its span existing.
    """

    connection = _FakeConnection(reconstruction=LOSSY_TEXT, fail_on="INSERT INTO source_spans")
    engine = _install_engine(monkeypatch, connection)

    outcome = asyncio.run(
        publish_document(_minimal_hierarchy(DOCUMENT_ID), _Mapper(), _repository())
    )

    # No refusal is reported, because none was committed.
    assert isinstance(outcome, RolledBackPublication)
    assert not isinstance(outcome, IntegrityRefusedPublication)
    assert outcome.references is None
    assert outcome.canonical_version_refs is None
    assert outcome.failure.stage == "repository_transaction"

    # The original still stands on its own; everything else went down together.
    # The run and the hierarchy were issued in the same block, and that block
    # rolled back, so neither persists -- which is the property that matters and
    # the one the old three-transaction shape could not provide.
    assert engine.block_with("INSERT INTO primary_sources").outcome == "commit"
    doomed = engine.block_with("INSERT INTO documents")
    assert doomed.outcome == "rollback"
    assert doomed.issued("INSERT INTO processing_runs"), (
        "the run must share the block it can be lost with, not a later one"
    )


# ----------------------------------------------------------------------
# 7. The inverse check: the matching path is untouched
# ----------------------------------------------------------------------


def test_the_matching_path_still_commits_hierarchy_run_and_state_in_one_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{xmpp}: a match is still one atomic publication with today's references."""

    connection = _FakeConnection()
    engine = _install_engine(monkeypatch, connection)

    references = _publish(connection)

    # Two blocks: the evidence prologue, then the one hierarchy block. Both
    # commit, and nothing is rolled back on the successful path.
    assert engine.blocks == 2
    assert engine.outcomes == ["commit", "commit"]
    assert engine.disposed == 1

    # The hierarchy, the run that produced it and the state it asserted are in
    # the *same* committed block, so a hierarchy without its run, or a run naming
    # a hierarchy that never committed, stays unrepresentable.
    hierarchy = engine.block_with("INSERT INTO documents")
    assert hierarchy.outcome == "commit"
    for statement in (
        "INSERT INTO chapters",
        "INSERT INTO paragraphs",
        "INSERT INTO semantic_chunks ",
        "INSERT INTO processing_runs",
        "INSERT INTO temporal_assertions",
        "INSERT INTO source_states",
    ):
        assert hierarchy.issued(statement), f"{statement!r} left the hierarchy block"

    run = hierarchy.one("INSERT INTO processing_runs")
    state = hierarchy.one("INSERT INTO source_states")
    original = engine.block_with("INSERT INTO primary_sources").one("INSERT INTO primary_sources")
    assert run["status"] == "succeeded"
    assert run["integrity_outcome"] == "match"
    assert run["source_sha256"] == run["reconstruction_sha256"] == SOURCE_SHA256
    assert run["first_difference_offset"] is None
    # A match locates nothing, cites no span and releases no context.
    assert run["mismatch_span_id"] is None
    assert run["diagnostic_authorization"] is None
    assert run["redaction_policy"] is None
    assert connection.issued("INSERT INTO source_spans") == []

    # Every key of today's reference dictionary, with today's values.
    plan = _plan()
    assert references == {
        "idempotent": False,
        "document_id": str(DOCUMENT_ID),
        "source_version_id": plan.source_version_id,
        "source_version": plan.source_version,
        "chapter_ids": (str(connection.one("INSERT INTO chapters")["id"]),),
        "paragraph_ids": (str(connection.one("INSERT INTO paragraphs")["id"]),),
        "chunk_ids": (str(FIRST_SENTENCE_ID), str(SECOND_SENTENCE_ID)),
        "chunking_strategy": "paragraph",
        "chunker": "svo",
        "primary_source_id": str(original["id"]),
        "processing_run_id": str(run["run_id"]),
        "processing_run_status": "succeeded",
        "integrity_outcome": "match",
        "source_state_assertion_id": str(state["assertion_id"]),
    }
    assert isinstance(references["document_id"], str)
    assert all(isinstance(value, str) for value in references["chunk_ids"])


def test_the_matching_path_is_reported_as_a_committed_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator still answers a match with references, not a refusal."""

    connection = _FakeConnection()
    _install_engine(monkeypatch, connection)

    outcome = asyncio.run(
        publish_document(_minimal_hierarchy(DOCUMENT_ID), _Mapper(), _repository())
    )

    assert not isinstance(outcome, (IntegrityRefusedPublication, RolledBackPublication))
    assert outcome.status == "committed"
    assert outcome.references["processing_run_status"] == "succeeded"
    assert outcome.references["integrity_outcome"] == "match"
    assert UUID(outcome.references["source_state_assertion_id"]).version == 4
