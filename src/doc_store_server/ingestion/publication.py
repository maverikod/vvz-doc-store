"""Orchestration boundary for publishing a completed ingestion aggregate.

The mapper and repository own the shape of the payload and the transaction,
respectively.  This module only orders identity lookup, mapping, and one
publication call, and turns failures into a typed result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, Protocol, TypeVar
from uuid import UUID

from .hierarchy_enrichment import CanonicalIngestionAggregate
from .integrity_diagnostics import IntegrityRefused, SourceSpanDiagnostic


PayloadT = TypeVar("PayloadT")
ReferenceT = TypeVar("ReferenceT")


@dataclass(frozen=True, slots=True)
class PublicationFailure:
    """Structured context for a publication failure."""

    stage: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class CanonicalVersionIdentity:
    """Identity used to detect a committed source-content-version replay."""

    source_upload_id: UUID
    source_version: str | int


@dataclass(frozen=True, slots=True)
class CommittedPublication(Generic[ReferenceT]):
    """A newly committed canonical document version."""

    status: str
    identity: CanonicalVersionIdentity
    canonical_version_refs: ReferenceT

    @property
    def references(self) -> ReferenceT:
        return self.canonical_version_refs


@dataclass(frozen=True, slots=True)
class IdempotentReplay(Generic[ReferenceT]):
    """A request for an already committed canonical document version."""

    status: str
    identity: CanonicalVersionIdentity
    canonical_version_refs: ReferenceT

    @property
    def references(self) -> ReferenceT:
        return self.canonical_version_refs


@dataclass(frozen=True, slots=True)
class RolledBackPublication:
    """A failed publication with no visible canonical version reference."""

    status: str
    identity: CanonicalVersionIdentity
    failure: PublicationFailure
    references: None = None

    @property
    def canonical_version_refs(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class IntegrityRefusedPublication:
    """A publication refused because its round trip did not match.

    This is deliberately not a ``RolledBackPublication`` and not a subclass of
    one.  A rollback reports that something went wrong -- a stage, an exception
    type, a message -- whereas a refusal is a verdict the system reached on
    purpose under ``{0o1l}``, and it carries what that requirement says the
    caller must be told: both SHA-256 values, the first differing byte offset or
    the strict-prefix length boundary, the bounded and redacted context released
    under a named authorization, and the Document, Chapter and SemanticChunk
    identifiers.  All of the latter live in ``diagnostic``.  Callers separate the
    two cases with ``isinstance``, so subclassing would silently make a refusal
    answer to the check for an incidental failure.

    Like a rollback, it exposes no canonical version reference: the hierarchy the
    refused publication would have made visible was rolled back, so there is
    nothing to point at.
    """

    status: Literal["integrity_refused"]
    identity: CanonicalVersionIdentity
    diagnostic: SourceSpanDiagnostic
    source_sha256: str
    reconstruction_sha256: str
    references: None = None
    evidence_recorded: bool = True
    """Whether the failed run and its ``SourceSpan`` were durably committed.

    A refusal is a measurement and stands on its own, so it is reported even
    when the transaction that records it failed.  ``diagnostic.span_id`` then
    names a row that does not exist, and this flag is what stops a caller from
    concluding the span was deleted rather than never written.
    """

    @property
    def canonical_version_refs(self) -> None:
        return None


PublicationOutcome = (
    CommittedPublication[ReferenceT]
    | IdempotentReplay[ReferenceT]
    | RolledBackPublication
    | IntegrityRefusedPublication
)


class PublicationMapperProtocol(Protocol[PayloadT]):
    """G-005 mapper contract consumed by the publication boundary."""

    def map(self, aggregate: CanonicalIngestionAggregate) -> PayloadT: ...


class PublicationRepositoryProtocol(Protocol[PayloadT, ReferenceT]):
    """Repository contract for identity lookup and one atomic publication."""

    async def find_committed_version(
        self, source_upload_id: UUID, source_version: str | int
    ) -> ReferenceT | None: ...

    async def publish_transaction(self, payload: PayloadT) -> ReferenceT: ...


def _identity(aggregate: CanonicalIngestionAggregate) -> CanonicalVersionIdentity:
    trace = aggregate.traceability
    return CanonicalVersionIdentity(trace.source_upload_id, trace.source_version)


def _failure(stage: str, error: Exception) -> PublicationFailure:
    return PublicationFailure(stage, type(error).__name__, str(error))


async def publish_document(
    aggregate: CanonicalIngestionAggregate,
    mapper: PublicationMapperProtocol[PayloadT],
    repository: PublicationRepositoryProtocol[PayloadT, ReferenceT],
) -> PublicationOutcome[ReferenceT]:
    """Publish one complete aggregate, or return a typed non-publication.

    Identity lookup happens before mapping and mutation.  A non-replay follows
    one path only: map once, then call the repository's atomic transaction once.

    A round trip the repository refused is reported as its own outcome rather
    than as a rollback: ``IntegrityRefused`` already carries the controlled
    ``{0o1l}`` payload, which the generic failure path would discard in favour of
    a stage string.  Every other path -- the identity lookup, the replay branch
    and genuine failures -- keeps its behaviour and its stage attribution.
    """

    identity = _identity(aggregate)
    try:
        existing = await repository.find_committed_version(
            identity.source_upload_id, identity.source_version
        )
        if existing is not None:
            return IdempotentReplay("idempotent_replay", identity, existing)

        payload = mapper.map(aggregate)
        references = await repository.publish_transaction(payload)
        return CommittedPublication("committed", identity, references)
    except IntegrityRefused as refusal:
        return IntegrityRefusedPublication(
            "integrity_refused",
            identity,
            refusal.diagnostic,
            refusal.source_sha256,
            refusal.reconstruction_sha256,
            None,
            refusal.evidence_recorded,
        )
    except Exception as error:
        stage = "identity_lookup"
        if "payload" in locals():
            stage = "repository_transaction" if "references" not in locals() else "publication"
        elif "existing" in locals():
            stage = "mapper"
        return RolledBackPublication("rolled_back", identity, _failure(stage, error))


publish = publish_document


__all__ = (
    "CanonicalVersionIdentity",
    "CommittedOutcome",
    "CommittedPublication",
    "IdempotentReplay",
    "IntegrityRefusedPublication",
    "PublicationFailure",
    "PublicationMapperProtocol",
    "PublicationOutcome",
    "PublicationRepositoryProtocol",
    "ReplayPublication",
    "RolledBackOutcome",
    "RolledBackPublication",
    "publish",
    "publish_document",
)


ReplayPublication = IdempotentReplay
CommittedOutcome = CommittedPublication
RolledBackOutcome = RolledBackPublication
