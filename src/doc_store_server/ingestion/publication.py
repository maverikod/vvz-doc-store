"""Orchestration boundary for publishing a completed ingestion aggregate.

The mapper and repository own the shape of the payload and the transaction,
respectively.  This module only orders identity lookup, mapping, and one
publication call, and turns failures into a typed result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from .hierarchy_enrichment import CanonicalIngestionAggregate


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


PublicationOutcome = (
    CommittedPublication[ReferenceT]
    | IdempotentReplay[ReferenceT]
    | RolledBackPublication
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
