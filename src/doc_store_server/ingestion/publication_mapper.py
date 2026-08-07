"""The production ``PublicationMapperProtocol`` implementation.

Mapping is the pure half of publishing an ingestion: it decides *what* a
document version's row payload is, never *whether* that version already exists
and never how it is written.  The replay lookup that used to sit in the middle
of this computation is the repository's, so this module opens no database
connection and imports no storage driver at all.

A ``CanonicalIngestionAggregate`` states the canonical hierarchy and its source
traceability, but it carries no operation id, no chunking strategy, no filename
and no raw text.  Those are facts of the normalized *request*, so the mapper is
constructed with that context once and maps each aggregate against it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from .hierarchy_enrichment import CanonicalIngestionAggregate
from .persistence_plan import PersistencePlan

CHECKSUM_ALGORITHM = "sha256"
"""The one digest algorithm every checksum in a plan is stated under."""

DEFAULT_PROJECT = "doc-store"
"""Project attributed to a source that names none of its own."""

UNTITLED_DOCUMENT = "Untitled document"
"""Title of last resort, used only when neither body nor filename gives one."""

_TITLE_LIMIT = 1024
_NAME_LIMIT = 512
_VERSION_MODULUS = 2_147_483_646

_PROJECT_PATTERN = re.compile(r"\bproject\s+([a-z0-9_-]+)\b")
_TAG_PATTERN = re.compile(r"\btag\s+([a-z0-9_-]+)\b")


@dataclass(frozen=True, slots=True)
class NormalizedRequestContext:
    """The normalized request facts a canonical aggregate does not carry.

    Every field here is already normalized: the text is decoded and canonical,
    the checksum is over those normalized bytes, and the chunking strategy is
    the resolved one rather than whatever the caller asked for.  The mapper
    therefore never re-normalizes, and mapping stays a total function of this
    context and one aggregate.
    """

    requested_document_id: UUID
    source_version_id: str
    normalized_source_version_id: str
    operation_id: str
    command: str
    text_value: str
    filename: str | None
    content_sha256: str
    chunking_strategy: str
    media_type: str | None = None
    byte_length: int | None = None


@dataclass(frozen=True, slots=True)
class _DerivedIdentity:
    """What a request context determines about the version being published.

    These are exactly the values that are computed rather than received, kept
    together so the plan assembly below is a naming step and nothing more.
    """

    document_id: UUID
    source_version: int
    title: str
    source_name: str | None
    length: int
    body_sha256: str
    file_id: UUID


def _stable_uuid4(value: str) -> UUID:
    """Derive a version-4 UUID deterministically from ``value``.

    The identity has to survive a replay of the same request, so it is a digest
    with the version and variant bits stamped in rather than a random draw.
    """

    raw = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _body_digest(text_value: str) -> str:
    """Digest of the normalized body, the plan's content identity."""

    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def _source_version_number(source_version_id: str) -> int:
    """Fold an opaque source version id into a stable positive integer."""

    digest = hashlib.sha256(source_version_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % _VERSION_MODULUS + 1


def _resolve_source_version(
    aggregate: CanonicalIngestionAggregate, source_version_id: str
) -> int:
    """Take the version the aggregate states, or fold the request's id.

    An aggregate whose traceability already names an integer version is the
    authority on which version is being published; a string version is opaque
    and is folded exactly as the request id would be, so both wirings agree.
    """

    stated = aggregate.traceability.source_version
    if isinstance(stated, int) and not isinstance(stated, bool) and stated > 0:
        return stated
    return _source_version_number(source_version_id)


def _source_name(filename: str | None) -> str | None:
    """Bare file name of a source, or ``None`` when the source is inline text."""

    if not filename:
        return None
    return PurePosixPath(filename).name[:_NAME_LIMIT]


def _title(filename: str | None, text_value: str) -> str:
    """First non-empty line of the body, unmarked, else the file name."""

    for line in text_value.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:_TITLE_LIMIT]
    return (_source_name(filename) or UNTITLED_DOCUMENT)[:_TITLE_LIMIT]


def _file_id(document_id: UUID, body_sha256: str, filename: str | None) -> UUID:
    """Identity of the File this version's bytes belong to.

    Document, content digest and name together decide it, so re-ingesting the
    same bytes under the same document names the same File instead of orphaning
    the previous one.
    """

    return _stable_uuid4(f"doc-store:file:{document_id}:{body_sha256}:{filename or ''}")


def _source_properties(text_value: str, default_project: str | None = None) -> dict[str, Any]:
    """Properties the source states about itself, in its own text."""

    lowered = text_value.lower()
    project_match = _PROJECT_PATTERN.search(lowered)
    tags = sorted(set(_TAG_PATTERN.findall(lowered)))
    project = project_match.group(1) if project_match else default_project
    result: dict[str, Any] = {"tags": tags}
    if project is not None:
        result["project"] = project
    return result


def _derive_identity(
    context: NormalizedRequestContext, source_version: int
) -> _DerivedIdentity:
    """Compute every value a plan needs that is not received verbatim."""

    body_sha256 = _body_digest(context.text_value)
    return _DerivedIdentity(
        document_id=context.requested_document_id,
        source_version=source_version,
        title=_title(context.filename, context.text_value),
        source_name=_source_name(context.filename),
        length=len(context.text_value),
        body_sha256=body_sha256,
        file_id=_file_id(context.requested_document_id, body_sha256, context.filename),
    )


def _doc_meta(
    context: NormalizedRequestContext, identity: _DerivedIdentity
) -> dict[str, Any]:
    """The document's ``block_meta`` mapping for this version.

    ``source_sha256``, ``file_sha256`` and ``body_sha256`` are deliberately the
    same digest under three names: readers of a stored document ask for the one
    that matches their own vocabulary, and disagreeing values would make the
    document look internally inconsistent.
    """

    body_sha256 = identity.body_sha256
    doc_meta: dict[str, Any] = {
        "file_id": str(identity.file_id),
        "source_version_id": context.source_version_id,
        "normalized_source_version_id": context.normalized_source_version_id,
        "operation_id": context.operation_id,
        "ingestion_command": context.command,
        "chunking_strategy": context.chunking_strategy,
        "checksum_algorithm": CHECKSUM_ALGORITHM,
        "content_sha256": context.content_sha256,
        "source_sha256": body_sha256,
        "file_sha256": body_sha256,
        "body_sha256": body_sha256,
    }
    doc_meta.update(_source_properties(context.text_value, default_project=DEFAULT_PROJECT))
    return doc_meta


def _build_plan(
    context: NormalizedRequestContext, identity: _DerivedIdentity
) -> PersistencePlan:
    """Name the received context and the derived identity as one payload."""

    return PersistencePlan(
        document_id=identity.document_id,
        source_version_id=context.source_version_id,
        normalized_source_version_id=context.normalized_source_version_id,
        operation_id=context.operation_id,
        command=context.command,
        text_value=context.text_value,
        filename=context.filename,
        content_sha256=context.content_sha256,
        chunking_strategy=context.chunking_strategy,
        source_version=identity.source_version,
        title=identity.title,
        source_name=identity.source_name,
        length=identity.length,
        body_sha256=identity.body_sha256,
        file_id=identity.file_id,
        doc_meta=_doc_meta(context, identity),
        media_type=context.media_type,
        byte_length=context.byte_length,
    )


class PublicationMapper:
    """Maps one canonical ingestion aggregate onto a ``PersistencePlan``.

    The mapper is deliberately storage-free and side-effect-free: given the same
    context and aggregate it returns an equal plan every time, which is what
    lets an idempotent replay be decided by the repository alone.
    """

    __slots__ = ("_context",)

    def __init__(
        self,
        *,
        requested_document_id: UUID,
        source_version_id: str,
        normalized_source_version_id: str,
        operation_id: str,
        command: str,
        text_value: str,
        filename: str | None,
        content_sha256: str,
        chunking_strategy: str,
        media_type: str | None = None,
        byte_length: int | None = None,
    ) -> None:
        self._context = NormalizedRequestContext(
            requested_document_id=requested_document_id,
            source_version_id=source_version_id,
            normalized_source_version_id=normalized_source_version_id,
            operation_id=operation_id,
            command=command,
            text_value=text_value,
            filename=filename,
            content_sha256=content_sha256,
            chunking_strategy=chunking_strategy,
            media_type=media_type,
            byte_length=byte_length,
        )

    @property
    def context(self) -> NormalizedRequestContext:
        """The normalized request this mapper maps aggregates against."""

        return self._context

    def map(self, aggregate: CanonicalIngestionAggregate) -> PersistencePlan:
        """Return the plan that publishes ``aggregate`` for this request."""

        context = self._context
        source_version = _resolve_source_version(aggregate, context.source_version_id)
        return _build_plan(context, _derive_identity(context, source_version))


__all__ = (
    "CHECKSUM_ALGORITHM",
    "DEFAULT_PROJECT",
    "NormalizedRequestContext",
    "PublicationMapper",
    "UNTITLED_DOCUMENT",
)
