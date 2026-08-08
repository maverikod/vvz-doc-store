"""The storage half of publication: one idempotency lookup and one transaction.

``PublicationRepository`` is the production implementation of
:class:`~doc_store_server.ingestion.publication.PublicationRepositoryProtocol`
with the payload bound to :class:`~doc_store_server.ingestion.persistence_plan.PersistencePlan`
and the reference bound to the mapping the ingestion boundary has always
returned to its callers.

It owns exactly two things, and they are deliberately asymmetric:

``find_committed_version`` decides whether this request was already answered.
It only reads, so it takes a plain connection and never opens a write
transaction; deciding "nothing to do" must not be able to change anything.

``publish_transaction`` writes the whole publication --- the immutable
``PrimarySource`` binding, the File and Document rows, the Chapter, Paragraph
and SemanticChunk hierarchy with its texts, versions, classifier assignments,
metrics, tokens and tags, then the measured round-trip verdict, the immutable
``ProcessingRun`` and the appended ``SourceState``.

It does so in three transactions, in an order that is dictated by what has to
survive a refusal rather than by style.  An *evidence prologue* commits the
immutable original alone, because the original's bytes were genuinely received
whether or not the hierarchy publishes and because ``processing_runs`` names it
under a ``RESTRICT`` foreign key --- a run written after a rollback would have no
valid parent otherwise.  The *hierarchy transaction* then writes everything the
publication makes visible and measures the round trip inside the same block: on
a match it writes the run and appends the state and commits, so a hierarchy
without the run that produced it, or a run naming a hierarchy that was rolled
back, stays unrepresentable; on a non-match it commits nothing, so the
incomplete version never becomes visible.  A *refusal-evidence transaction*
finally records the failed run and its ``SourceSpan``, which is exactly the
evidence that must outlive the rollback, and the refusal is raised.

The SQL, the reference keys and the status values are the ones the runtime
boundary already used; the module-level helpers that issue that SQL are
imported from it rather than copied, so there remains one definition of each.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from doc_store_server.ingestion import integrity_diagnostics, runtime_boundary
from doc_store_server.ingestion.persistence_plan import PersistencePlan
from doc_store_server.ingestion.svo_chunking import RuntimeChunk, SvoRuntimeChunker


ReferenceMapping = dict[str, Any]
"""The canonical-version reference dictionary returned to ingestion callers."""

DIAGNOSTIC_AUTHORIZATION = "owner"
"""The authorization decision a refusal produced by ingestion is released under.

Ingestion runs on behalf of whoever offered the bytes, so the one party entitled
to see where their own source and its reconstruction part company is that
submitter.  It is fixed here rather than taken from the request for the same
reason ``runtime_boundary.DIAGNOSTIC_CONTEXT_LIMIT`` is: a caller who could name
their own authorization could grant themselves context.
"""

DIAGNOSTIC_REDACTION_POLICY = "redact_pii"
"""The policy applied to the context a refusal discloses.

The bounded window is taken from the source itself, so it is redacted before it
is stored even though the authorization allows it to be seen at all; disclosure
on an error path fails towards withholding.
"""


class _RefusedRoundTrip(Exception):
    """Private signal that leaves the hierarchy block without committing it.

    It never escapes ``_publish``.  Rolling a transaction back means raising out
    of its context manager, but the caller must receive
    ``integrity_diagnostics.IntegrityRefused`` and only after the refusal
    evidence has been committed, so the two are deliberately different types:
    this one carries the measured verdict across the rollback boundary, and the
    public one is raised on the far side of it.
    """

    __slots__ = (
        "diagnostic",
        "integrity_outcome",
        "first_difference_offset",
        "reconstruction_sha256",
    )

    def __init__(
        self,
        diagnostic: integrity_diagnostics.SourceSpanDiagnostic,
        *,
        integrity_outcome: str,
        first_difference_offset: int | None,
        reconstruction_sha256: str,
    ) -> None:
        self.diagnostic = diagnostic
        self.integrity_outcome = integrity_outcome
        self.first_difference_offset = first_difference_offset
        self.reconstruction_sha256 = reconstruction_sha256
        super().__init__("the measured round trip did not match")


class PublicationRepository:
    """Publish one canonical document version, or report it already exists."""

    __slots__ = ("_database_url", "_chunker")

    def __init__(
        self,
        database_url: str,
        *,
        chunker: SvoRuntimeChunker | None = None,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._chunker = chunker

    # ------------------------------------------------------------------
    # Identity lookup
    # ------------------------------------------------------------------

    async def find_committed_version(
        self,
        source_upload_id: UUID,
        source_version: str | int,
        *,
        plan: PersistencePlan | None = None,
    ) -> ReferenceMapping | None:
        """Return the reference for an already committed version, or ``None``.

        Three outcomes are recognised, exactly as the ingestion boundary
        recognised them before: a File whose stored checksum already matches
        the request (``file_checksum_match``), a Document already carrying this
        source version (``source_version_match``), and a Document that is
        current but still awaiting revectorization, which is reported with
        ``idempotent`` false so the caller keeps going
        (``revectorization_pending``).

        ``plan`` is optional.  When the caller already mapped the request it
        supplies the plan, and the incoming checksums, filename and command are
        compared exactly as before.  When only the canonical identity is known
        --- the path :func:`publication.publish_document` takes --- the stored
        Document row supplies the source version identity and chunking strategy
        instead, and the checksum comparison, which has no incoming value to
        compare against, simply cannot fire.
        """

        return await asyncio.to_thread(
            self._find_committed_version,
            source_upload_id,
            source_version,
            plan,
        )

    def _find_committed_version(
        self,
        source_upload_id: UUID,
        source_version: str | int,
        plan: PersistencePlan | None,
    ) -> ReferenceMapping | None:
        engine = self._engine()
        try:
            # Read-only: a lookup that decides nothing needs doing must not be
            # able to leave anything behind, so it never opens a write block.
            with engine.connect() as connection:
                return self._replay_reference(
                    connection,
                    document_id=source_upload_id,
                    source_version=source_version,
                    plan=plan,
                )
        finally:
            engine.dispose()

    def _replay_reference(
        self,
        connection: Any,
        *,
        document_id: UUID,
        source_version: str | int,
        plan: PersistencePlan | None,
    ) -> ReferenceMapping | None:
        if plan is not None and plan.command == "document_chunk":
            # A rechunk is an explicit request to redo the hierarchy; it is
            # never answered from what is already stored.
            return None
        state = runtime_boundary._load_document_file_state(connection, document_id)
        if state is None:
            return None
        quiescent = (
            state["file_needs_rechunk"] is False
            and state["file_needs_revectorize"] is False
            and state["document_needs_revectorize"] is False
        )
        if (
            plan is not None
            and plan.filename is not None
            and quiescent
            and (
                state["file_content_sha256"] == plan.content_sha256
                or state["file_body_sha256"] == plan.body_sha256
            )
        ):
            return runtime_boundary._existing_references(
                connection,
                document_id=document_id,
                source_version_id=plan.source_version_id,
                source_version=plan.source_version,
                chunking_strategy=plan.chunking_strategy,
                idempotent_reason="file_checksum_match",
            )

        stored = self._committed_document_row(
            connection,
            document_id=document_id,
            source_version=source_version,
            plan=plan,
        )
        if stored is None:
            return None
        source_version_id = stored["source_version_id"]
        if source_version_id is None:
            return None
        chunking_strategy = stored["chunking_strategy"]
        if chunking_strategy is None:
            return None
        version_number = (
            plan.source_version if plan is not None else _version_number(source_version)
        )
        if quiescent:
            return runtime_boundary._existing_references(
                connection,
                document_id=document_id,
                source_version_id=source_version_id,
                source_version=version_number,
                chunking_strategy=chunking_strategy,
                idempotent_reason="source_version_match",
            )
        if state["file_needs_rechunk"] is False and (
            state["file_needs_revectorize"] is True
            or state["document_needs_revectorize"] is True
        ):
            return runtime_boundary._existing_references(
                connection,
                document_id=document_id,
                source_version_id=source_version_id,
                source_version=version_number,
                chunking_strategy=chunking_strategy,
                idempotent=False,
                idempotent_reason="revectorization_pending",
            )
        return None

    @staticmethod
    def _committed_document_row(
        connection: Any,
        *,
        document_id: UUID,
        source_version: str | int,
        plan: PersistencePlan | None,
    ) -> Mapping[str, Any] | None:
        """Find the stored Document that already carries this source version."""

        if plan is not None:
            row = connection.execute(
                text(
                    "SELECT id, block_meta->>'chunking_strategy' AS chunking_strategy "
                    "FROM documents "
                    "WHERE id = :document_id "
                    "AND block_meta->>'source_version_id' = :source_version_id"
                ),
                {"document_id": document_id, "source_version_id": plan.source_version_id},
            ).mappings().one_or_none()
            if row is None:
                return None
            return {
                "source_version_id": plan.source_version_id,
                "chunking_strategy": row["chunking_strategy"] or plan.chunking_strategy,
            }
        row = connection.execute(
            text(
                "SELECT block_meta->>'source_version_id' AS source_version_id, "
                "block_meta->>'chunking_strategy' AS chunking_strategy "
                "FROM documents "
                "WHERE id = :document_id AND source_version = :source_version"
            ),
            {"document_id": document_id, "source_version": _version_number(source_version)},
        ).mappings().one_or_none()
        if row is None:
            return None
        return {
            "source_version_id": row["source_version_id"],
            "chunking_strategy": row["chunking_strategy"],
        }

    # ------------------------------------------------------------------
    # Publication
    # ------------------------------------------------------------------

    async def publish_transaction(
        self,
        payload: PersistencePlan,
        paragraph_chunks: Sequence[RuntimeChunk] | None = None,
        sentence_chunks: Sequence[RuntimeChunk] | None = None,
    ) -> ReferenceMapping:
        """Write one whole publication atomically and return its references.

        The chunk units may be supplied by a caller that already produced them;
        otherwise they are derived from the payload text with the configured
        chunker, so the protocol's single-argument call is self-sufficient.
        """

        if paragraph_chunks is None or sentence_chunks is None:
            paragraph_chunks, sentence_chunks = await self._chunk_units(payload)
        return await asyncio.to_thread(
            self._publish,
            payload,
            paragraph_chunks,
            sentence_chunks,
        )

    async def _chunk_units(
        self,
        payload: PersistencePlan,
    ) -> tuple[Sequence[RuntimeChunk], Sequence[RuntimeChunk]]:
        chunker = self._chunker or runtime_boundary.installed_svo_runtime_chunker()
        source_id = str(payload.document_id)
        paragraph_chunks = await chunker.chunk(
            text=payload.text_value,
            strategy="paragraph",
            source_id=source_id,
        )
        sentence_chunks = await runtime_boundary._sentence_chunks_from_paragraph_batch(
            chunker=chunker,
            paragraph_chunks=paragraph_chunks,
            source_id=source_id,
        )
        return paragraph_chunks, sentence_chunks

    def _publish(
        self,
        payload: PersistencePlan,
        paragraph_chunks: Sequence[RuntimeChunk],
        sentence_chunks: Sequence[RuntimeChunk],
    ) -> ReferenceMapping:
        """Publish one hierarchy, or refuse it and keep the evidence of refusing.

        Three transactions run in a fixed order, and which of them commits is
        the whole of the publication semantics.

        The first commits the immutable ``PrimarySource`` alone.  It is not a
        stylistic split: ``processing_runs.primary_source_id`` is ``NOT NULL``
        under a ``RESTRICT`` foreign key, so a failed run written after the
        hierarchy has been rolled back would have no valid parent unless the
        original is already committed.  It is also the truthful record --- the
        original's bytes were received whether or not anything is published ---
        and it is replay-safe, because ``_resolve_primary_source`` mints a
        stable UUID and inserts ``ON CONFLICT (id) DO NOTHING``.

        The second writes everything the publication makes visible: the File and
        Document rows, the Chapter, Paragraph and SemanticChunk hierarchy, and
        then the measured verdict.  The verdict is measured, not assumed: the
        normalized source is compared against the same scope read back through
        the reconstruction rule ``source_file_reconstruct`` applies, so
        ``status = 'succeeded'`` (which the database only accepts on equal
        hashes) is earned.  On a match the ``ProcessingRun`` and the appended
        ``SourceState`` are written inside this same block, so the successful
        path keeps its atomicity exactly as before.  On a mismatch or a strict
        prefix nothing here commits: the block is left by raising, the whole
        hierarchy is rolled back, and the incomplete version never becomes
        visible under ``{xmpp}``.

        The third runs only after such a refusal and writes the failed run and
        its ``SourceSpan``, which is the audit evidence that has to outlive the
        rollback.  That run leaves ``document_id``, ``chapter_id`` and
        ``chunk_id`` null, because migration 0019 declares all three as
        ``SET NULL`` foreign keys into rows the rollback has just removed; the
        identifiers ``{0o1l}`` demands ride on the ``SourceSpan``, whose own
        columns carry no such reference, and on the raised refusal.
        """

        if not paragraph_chunks:
            raise ValueError("chunker returned no paragraph chunks")
        if not sentence_chunks:
            raise ValueError("chunker returned no sentence chunks")
        document_id = payload.document_id
        run_id = uuid4()
        run_started_at = datetime.now(timezone.utc)
        chapter_id = uuid4()
        chapter_ids = [str(chapter_id)]
        paragraph_ids: list[str] = []
        chunk_ids: list[str] = []
        chunk_spans: list[tuple[UUID, int]] = []
        engine = self._engine()
        try:
            # (1) The evidence prologue: the original is committed on its own so
            # that a refused run still has the parent its foreign key demands.
            with engine.begin() as connection:
                primary_source_id = runtime_boundary._resolve_primary_source(connection, payload)
            try:
                # (2) The hierarchy, and the verdict measured over it.
                with engine.begin() as connection:
                    runtime_boundary._mark_existing_hierarchy_deleted(connection, document_id)
                    _retire_previous_files(connection, document_id)
                    _write_file(connection, payload, primary_source_id)
                    _write_document(connection, payload)
                    _write_chapter(connection, payload, chapter_id)
                    reconstruction_length = 0
                    previous_paragraph_id: UUID | None = None
                    for order_index, paragraph_chunk in enumerate(paragraph_chunks):
                        paragraph_id = uuid4()
                        paragraph_ids.append(str(paragraph_id))
                        paragraph_meta = _write_paragraph(
                            connection,
                            payload,
                            paragraph_chunk=paragraph_chunk,
                            paragraph_id=paragraph_id,
                            chapter_id=chapter_id,
                            order_index=order_index,
                        )
                        for sentence_chunk in runtime_boundary._sentence_chunks_for_paragraph(
                            paragraph_chunk,
                            sentence_chunks,
                        ):
                            written_chunk_id = _write_semantic_chunk(
                                connection,
                                payload,
                                sentence_chunk=sentence_chunk,
                                paragraph_id=paragraph_id,
                                chapter_id=chapter_id,
                                paragraph_meta=paragraph_meta,
                            )
                            chunk_ids.append(str(written_chunk_id))
                            # The payload is accumulated exactly as the
                            # reconstruction will join it, so a byte offset into
                            # the comparison can later be attributed to a chunk
                            # without reading anything back.
                            reconstruction_length += len(
                                _reconstruction_separator(
                                    previous_paragraph_id, paragraph_id
                                ).encode("utf-8")
                            )
                            reconstruction_length += len(sentence_chunk.text.encode("utf-8"))
                            chunk_spans.append((written_chunk_id, reconstruction_length))
                            previous_paragraph_id = paragraph_id
                    reconstructed = runtime_boundary._reconstructed_scope_text(
                        connection, document_id
                    )
                    reconstruction_sha256 = hashlib.sha256(
                        reconstructed.encode("utf-8")
                    ).hexdigest()
                    integrity_outcome, first_difference_offset = (
                        runtime_boundary._integrity_verdict(payload.text_value, reconstructed)
                    )
                    if integrity_outcome != integrity_diagnostics.MATCHING_OUTCOME:
                        raise _RefusedRoundTrip(
                            integrity_diagnostics.build_integrity_diagnostic(
                                source_text=payload.text_value,
                                reconstructed_text=reconstructed,
                                integrity_outcome=integrity_outcome,
                                first_difference_offset=first_difference_offset,
                                document_id=document_id,
                                chapter_id=chapter_id,
                                chunk_id=_chunk_at_offset(chunk_spans, first_difference_offset),
                                authorization=DIAGNOSTIC_AUTHORIZATION,
                                redaction_policy=DIAGNOSTIC_REDACTION_POLICY,
                                context_limit=runtime_boundary.DIAGNOSTIC_CONTEXT_LIMIT,
                            ),
                            integrity_outcome=integrity_outcome,
                            first_difference_offset=first_difference_offset,
                            reconstruction_sha256=reconstruction_sha256,
                        )
                    run_status = "succeeded"
                    runtime_boundary._insert_processing_run(
                        connection,
                        prepared=payload,
                        run_id=run_id,
                        primary_source_id=primary_source_id,
                        chapter_id=chapter_id,
                        status=run_status,
                        source_sha256=payload.body_sha256,
                        reconstruction_sha256=reconstruction_sha256,
                        integrity_outcome=integrity_outcome,
                        first_difference_offset=first_difference_offset,
                        started_at=run_started_at,
                        completed_at=datetime.now(timezone.utc),
                    )
                    assertion_id = runtime_boundary._append_source_state(
                        connection,
                        prepared=payload,
                        run_id=run_id,
                        primary_source_id=primary_source_id,
                        reconstruction_sha256=reconstruction_sha256,
                        chapter_ids=chapter_ids,
                        paragraph_count=len(paragraph_ids),
                        chunk_count=len(chunk_ids),
                    )
            except _RefusedRoundTrip as refused:
                # (3) The refusal evidence, committed after the rollback so that
                # what was refused, and where, remains on the record.
                diagnostic = refused.diagnostic
                with engine.begin() as connection:
                    runtime_boundary._insert_processing_run(
                        connection,
                        prepared=payload,
                        run_id=run_id,
                        primary_source_id=primary_source_id,
                        status="failed",
                        source_sha256=payload.body_sha256,
                        reconstruction_sha256=refused.reconstruction_sha256,
                        integrity_outcome=refused.integrity_outcome,
                        first_difference_offset=refused.first_difference_offset,
                        started_at=run_started_at,
                        completed_at=datetime.now(timezone.utc),
                        mismatch_span_id=diagnostic.span_id,
                        diagnostic_authorization=diagnostic.authorization_decision,
                        redaction_policy=diagnostic.redaction_policy,
                        document_id=None,
                        chapter_id=None,
                        chunk_id=None,
                    )
                    runtime_boundary._insert_source_span(
                        connection,
                        diagnostic=diagnostic,
                        run_id=run_id,
                    )
                raise integrity_diagnostics.IntegrityRefused(
                    diagnostic,
                    source_sha256=payload.body_sha256,
                    reconstruction_sha256=refused.reconstruction_sha256,
                ) from None
            return {
                "idempotent": False,
                "document_id": str(document_id),
                "source_version_id": payload.source_version_id,
                "source_version": payload.source_version,
                "chapter_ids": tuple(chapter_ids),
                "paragraph_ids": tuple(paragraph_ids),
                "chunk_ids": tuple(chunk_ids),
                "chunking_strategy": payload.chunking_strategy,
                "chunker": "svo",
                "primary_source_id": str(primary_source_id),
                "processing_run_id": str(run_id),
                "processing_run_status": run_status,
                "integrity_outcome": integrity_outcome,
                "source_state_assertion_id": str(assertion_id),
            }
        finally:
            engine.dispose()

    def _engine(self) -> Engine:
        return create_engine(self._database_url, pool_pre_ping=True)


def _reconstruction_separator(
    previous_paragraph_id: UUID | None,
    paragraph_id: UUID,
) -> str:
    """Return the separator the reconstruction puts before the next payload.

    The rule is ``_reconstructed_scope_text``'s own: nothing before the first
    payload, a space between payloads of one paragraph, and a blank line between
    paragraphs.  The separators themselves come from the runtime boundary, so
    there is still exactly one definition of them.
    """

    if previous_paragraph_id is None:
        return ""
    if previous_paragraph_id == paragraph_id:
        return runtime_boundary.RECONSTRUCTION_SEPARATORS["within_paragraph"]
    return runtime_boundary.RECONSTRUCTION_SEPARATORS["between_paragraphs"]


def _chunk_at_offset(
    chunk_spans: Sequence[tuple[UUID, int]],
    first_difference_offset: int | None,
) -> UUID | None:
    """Name the chunk the first differing byte offset falls in.

    ``chunk_spans`` pairs each written chunk with the byte position at which its
    payload ends in the reconstruction, accumulated in the order the
    reconstruction joins them, so the first span reaching past the offset is the
    chunk that contains it.  Bytes belonging to a separator are attributed to the
    payload that follows them, and an offset at or past the end --- which is what
    a strict-prefix length boundary produces when the reconstruction is the
    shorter text --- is attributed to the last chunk written, because that is
    where the reconstruction stopped.
    """

    if not chunk_spans or first_difference_offset is None:
        return None
    for chunk_id, payload_end in chunk_spans:
        if first_difference_offset < payload_end:
            return chunk_id
    return chunk_spans[-1][0]


def _version_number(source_version: str | int) -> int:
    """Resolve a canonical source version to the integer ``documents`` stores."""

    if isinstance(source_version, int):
        return source_version
    value = str(source_version)
    try:
        return int(value)
    except ValueError:
        return runtime_boundary._source_version_number(value)


def _retire_previous_files(connection: Any, document_id: UUID) -> None:
    connection.execute(
        text(
            "UPDATE files SET is_deleted = TRUE, deleted_at = now(), updated_at = now() "
            "WHERE owner_id = :document_id "
            "OR id = (SELECT owner_id FROM documents WHERE id = :document_id)"
        ),
        {"document_id": document_id},
    )


def _write_file(connection: Any, payload: PersistencePlan, primary_source_id: UUID) -> None:
    """Write the File bound, once and for good, to one immutable original.

    ``primary_source_id`` is required and unique: a File states which immutable
    original it represents, or it does not exist.  The conflict branch
    deliberately leaves the column alone, because a File never swaps the
    original it was accepted with.
    """

    connection.execute(
        text(
            "INSERT INTO files "
            "(id, owner_id, primary_source_id, path, name, media_type, byte_length, "
            "char_count, checksum_algorithm, content_sha256, body_sha256, "
            "needs_revectorize, needs_rechunk, is_deleted, deleted_at, block_meta) "
            "VALUES (:id, NULL, :primary_source_id, :path, :name, 'text/plain', NULL, "
            ":char_count, 'sha256', :content_sha256, :body_sha256, TRUE, FALSE, FALSE, "
            "NULL, CAST(:block_meta AS jsonb)) "
            "ON CONFLICT (id) DO UPDATE SET "
            "path = EXCLUDED.path, "
            "name = EXCLUDED.name, "
            "media_type = EXCLUDED.media_type, "
            "char_count = EXCLUDED.char_count, "
            "content_sha256 = EXCLUDED.content_sha256, "
            "body_sha256 = EXCLUDED.body_sha256, "
            "needs_revectorize = TRUE, "
            "needs_rechunk = FALSE, "
            "is_deleted = FALSE, "
            "deleted_at = NULL, "
            "updated_at = now(), "
            "block_meta = EXCLUDED.block_meta"
        ),
        {
            "id": payload.file_id,
            "primary_source_id": primary_source_id,
            "path": payload.filename or payload.source_name or str(payload.file_id),
            "name": payload.source_name or payload.filename or str(payload.file_id),
            "char_count": payload.length,
            "content_sha256": payload.content_sha256,
            "body_sha256": payload.body_sha256,
            "block_meta": json.dumps(dict(payload.doc_meta)),
        },
    )


def _write_document(connection: Any, payload: PersistencePlan) -> None:
    connection.execute(
        text(
            "INSERT INTO documents "
            "(id, owner_id, source_upload_id, source_version, source_path, source_name, source_hash, "
            "checksum_algorithm, content_sha256, body_sha256, title, processing_status, "
            "processing_attempt, needs_revectorize, processing_trace_id, "
            "processing_started_at, processing_completed_at, block_meta) "
            "VALUES (:id, :owner_id, :source_upload_id, :source_version, :source_path, :source_name, "
            ":source_hash, 'sha256', :content_sha256, :body_sha256, :title, 'draft', "
            "1, TRUE, :trace_id, now(), NULL, "
            "CAST(:block_meta AS jsonb)) "
            "ON CONFLICT (id) DO UPDATE SET "
            "owner_id = EXCLUDED.owner_id, "
            "source_upload_id = EXCLUDED.source_upload_id, "
            "source_version = EXCLUDED.source_version, "
            "source_path = EXCLUDED.source_path, "
            "source_name = EXCLUDED.source_name, "
            "source_hash = EXCLUDED.source_hash, "
            "checksum_algorithm = EXCLUDED.checksum_algorithm, "
            "content_sha256 = EXCLUDED.content_sha256, "
            "body_sha256 = EXCLUDED.body_sha256, "
            "title = EXCLUDED.title, "
            "processing_status = 'draft', "
            "processing_attempt = documents.processing_attempt + 1, "
            "needs_revectorize = TRUE, "
            "processing_trace_id = EXCLUDED.processing_trace_id, "
            "processing_started_at = EXCLUDED.processing_started_at, "
            "processing_completed_at = EXCLUDED.processing_completed_at, "
            "updated_at = now(), "
            "deleted_at = NULL, "
            "block_meta = EXCLUDED.block_meta"
        ),
        {
            "id": payload.document_id,
            "owner_id": payload.file_id,
            "source_upload_id": payload.document_id,
            "source_version": payload.source_version,
            "source_path": payload.filename,
            "source_name": payload.source_name,
            "source_hash": payload.body_sha256,
            "content_sha256": payload.content_sha256,
            "body_sha256": payload.body_sha256,
            "title": payload.title,
            "trace_id": UUID(payload.operation_id),
            "block_meta": json.dumps(dict(payload.doc_meta)),
        },
    )


def _write_chapter(connection: Any, payload: PersistencePlan, chapter_id: UUID) -> None:
    connection.execute(
        text(
            "INSERT INTO chapters "
            "(id, owner_id, document_id, order_index, heading, level, source_start, source_end, "
            "block_meta) "
            "VALUES (:id, :document_id, :document_id, 0, :heading, 1, 0, :source_end, "
            "CAST(:block_meta AS jsonb))"
        ),
        {
            "id": chapter_id,
            "document_id": payload.document_id,
            "heading": payload.title,
            "source_end": payload.length,
            "block_meta": json.dumps(dict(payload.doc_meta)),
        },
    )


def _write_paragraph(
    connection: Any,
    payload: PersistencePlan,
    *,
    paragraph_chunk: RuntimeChunk,
    paragraph_id: UUID,
    chapter_id: UUID,
    order_index: int,
) -> dict[str, Any]:
    """Write one paragraph and return the metadata its chunks inherit."""

    paragraph_text = paragraph_chunk.text
    chunk_features = runtime_boundary._chunk_features(
        paragraph_text,
        source_name=payload.source_name,
        chunking_strategy="paragraph",
    )
    chunk_features.update(runtime_boundary._chunk_metadata_features(paragraph_chunk.metadata))
    paragraph_meta = {
        **dict(payload.doc_meta),
        **runtime_boundary._source_properties(paragraph_text),
        **chunk_features,
        "paragraph_number": order_index + 1,
        "chunker": "svo",
    }
    connection.execute(
        text(
            "INSERT INTO paragraphs "
            "(id, owner_id, document_id, chapter_id, order_index, text, source_start, "
            "source_end, search_weight, block_meta) "
            "VALUES (:id, :chapter_id, :document_id, :chapter_id, :order_index, :body, "
            ":source_start, :source_end, 1, CAST(:block_meta AS jsonb))"
        ),
        {
            "id": paragraph_id,
            "document_id": payload.document_id,
            "chapter_id": chapter_id,
            "order_index": order_index,
            "body": paragraph_text,
            "source_start": paragraph_chunk.start,
            "source_end": paragraph_chunk.end,
            "block_meta": json.dumps(paragraph_meta),
        },
    )
    return paragraph_meta


def _write_semantic_chunk(
    connection: Any,
    payload: PersistencePlan,
    *,
    sentence_chunk: RuntimeChunk,
    paragraph_id: UUID,
    chapter_id: UUID,
    paragraph_meta: Mapping[str, Any],
) -> UUID:
    """Write one chunk with its text, version, classifiers, metrics and index rows."""

    sentence_text = sentence_chunk.text
    chunk_id = sentence_chunk.uuid
    sentence_features = runtime_boundary._chunk_features(
        sentence_text,
        source_name=payload.source_name,
        chunking_strategy="sentence",
    )
    sentence_features.update(runtime_boundary._chunk_metadata_features(sentence_chunk.metadata))
    sentence_features["block_type"] = "sentence"
    classifier_values = runtime_boundary._semantic_classifier_values(sentence_features)
    dictionary_ids = runtime_boundary._semantic_dictionary_ids(connection, classifier_values)
    chunk_meta = {
        **dict(paragraph_meta),
        **sentence_features,
        "unit_type": "sentence",
        "chapter_id": str(chapter_id),
        "paragraph_id": str(paragraph_id),
        "source_name": payload.source_name,
        "svo_chunk": dict(sentence_chunk.metadata),
        **classifier_values,
    }
    connection.execute(
        text(
            "INSERT INTO semantic_chunks "
            "(id, owner_id, document_id, paragraph_id, chapter_id, order_index, text, "
            "source_start, source_end, char_count, chunk_type, chunk_type_id, "
            "role_id, status_id, block_type_id, language_id, category_id, "
            "search_weight, block_meta) "
            "VALUES (:id, :paragraph_id, :document_id, :paragraph_id, :chapter_id, "
            ":order_index, '', :source_start, :source_end, :char_count, "
            ":chunk_type, :chunk_type_id, :role_id, :status_id, "
            ":block_type_id, :language_id, :category_id, 1, "
            "CAST(:block_meta AS jsonb))"
        ),
        {
            "id": chunk_id,
            "document_id": payload.document_id,
            "paragraph_id": paragraph_id,
            "chapter_id": chapter_id,
            "order_index": sentence_chunk.ordinal,
            "source_start": sentence_chunk.start,
            "source_end": sentence_chunk.end,
            "char_count": len(sentence_text),
            "chunk_type": classifier_values["type"],
            "chunk_type_id": dictionary_ids["chunk_type_id"],
            "role_id": dictionary_ids["role_id"],
            "status_id": dictionary_ids["status_id"],
            "block_type_id": dictionary_ids["block_type_id"],
            "language_id": dictionary_ids["language_id"],
            "category_id": dictionary_ids["category_id"],
            "block_meta": json.dumps(chunk_meta),
        },
    )
    text_sha256 = hashlib.sha256(sentence_text.encode("utf-8")).hexdigest()
    chunk_meta_json = json.dumps(chunk_meta)
    connection.execute(
        text(
            "INSERT INTO semantic_chunk_texts "
            "(chunk_uuid, text, text_sha256, char_count, block_meta) "
            "VALUES (:chunk_uuid, :body, :text_sha256, :char_count, "
            "CAST(:block_meta AS jsonb))"
        ),
        {
            "chunk_uuid": chunk_id,
            "body": sentence_text,
            "text_sha256": text_sha256,
            "char_count": len(sentence_text),
            "block_meta": chunk_meta_json,
        },
    )
    runtime_boundary._insert_semantic_chunk_initial_version(
        connection,
        chunk_id,
        text_value=sentence_text,
        text_sha256=text_sha256,
        char_count=len(sentence_text),
        block_meta=chunk_meta_json,
    )
    runtime_boundary._upsert_semantic_chunk_classifier_assignments(
        connection,
        chunk_id,
        dictionary_ids,
    )
    runtime_boundary._insert_semantic_chunk_default_metrics(connection, chunk_id)
    _write_chunk_index_rows(connection, chunk_id, sentence_features)
    return chunk_id


def _write_chunk_index_rows(
    connection: Any,
    chunk_id: UUID,
    sentence_features: Mapping[str, Any],
) -> None:
    for token_kind in ("tokens", "bm25_tokens"):
        for token_ordinal, token_value in enumerate(sentence_features[token_kind]):
            connection.execute(
                text(
                    "INSERT INTO semantic_chunk_tokens "
                    "(chunk_uuid, token_kind, ordinal, token_value) "
                    "VALUES (:chunk_uuid, :token_kind, :ordinal, :token_value)"
                ),
                {
                    "chunk_uuid": chunk_id,
                    "token_kind": token_kind,
                    "ordinal": token_ordinal,
                    "token_value": token_value,
                },
            )
    for tag_ordinal, tag_value in enumerate(sentence_features["tags"]):
        connection.execute(
            text(
                "INSERT INTO semantic_chunk_tags "
                "(chunk_uuid, ordinal, tag_value) "
                "VALUES (:chunk_uuid, :ordinal, :tag_value)"
            ),
            {"chunk_uuid": chunk_id, "ordinal": tag_ordinal, "tag_value": tag_value},
        )


__all__ = ("PublicationRepository", "ReferenceMapping")
