"""PostgreSQL persistence mappings for the canonical document hierarchy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship


metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class Base(DeclarativeBase):
    """Shared declarative base imported by migrations and repositories."""

    metadata = metadata


UUID4 = PGUUID(as_uuid=True)


class EntityCRUDMixin:
    """Virtual CRUD/lifecycle contract shared by addressable entities."""

    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @classmethod
    def get(cls, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @classmethod
    def list(cls, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def update(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @classmethod
    def soft_delete(cls, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @classmethod
    def undelete(cls, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @classmethod
    def hard_delete(cls, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class DictionaryCRUDMixin(EntityCRUDMixin):
    """Shared CRUD/lifecycle shape for small UUID-backed dictionaries."""

    __abstract__ = True

    @declared_attr
    def __table_args__(cls) -> tuple[Any, ...]:
        table = cls.__tablename__
        return (
            UniqueConstraint("descr", name=f"uq_{table}_descr"),
            Index(f"ix_{table}_lifecycle", "is_deleted", "deleted_at"),
        )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    descr: Mapped[str] = mapped_column(String(100), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChunkTypeDictionary(DictionaryCRUDMixin, Base):
    """Dictionary of adapter SemanticChunk.type values."""

    __tablename__ = "chunk_types"


class ChunkRoleDictionary(DictionaryCRUDMixin, Base):
    """Dictionary of adapter SemanticChunk.role values."""

    __tablename__ = "chunk_roles"


class ChunkStatusDictionary(DictionaryCRUDMixin, Base):
    """Dictionary of adapter SemanticChunk.status values."""

    __tablename__ = "chunk_statuses"


class BlockTypeDictionary(DictionaryCRUDMixin, Base):
    """Dictionary of adapter SemanticChunk.block_type values."""

    __tablename__ = "block_types"


class LanguageDictionary(DictionaryCRUDMixin, Base):
    """Dictionary of adapter SemanticChunk.language values."""

    __tablename__ = "languages"


class CategoryDictionary(DictionaryCRUDMixin, Base):
    """Dictionary of SemanticChunk category classifier values."""

    __tablename__ = "categories"


REGISTRY_KIND_LOCATORS: Mapping[str, str] = MappingProxyType(
    {
        "document": "documents",
        "chapter": "chapters",
        "paragraph": "paragraphs",
        "semantic_chunk": "semantic_chunks",
        "file": "files",
        "content_attachment": "content_attachments",
        "project": "projects",
        "chunk_type": "chunk_types",
        "chunk_role": "chunk_roles",
        "chunk_status": "chunk_statuses",
        "block_type": "block_types",
        "language": "languages",
        "category": "categories",
    }
)
"""Allowlisted registry kind to concrete typed-table locator pairs."""

REGISTRY_KINDS: tuple[str, ...] = tuple(REGISTRY_KIND_LOCATORS)
REGISTRY_TABLE_LOCATORS: tuple[str, ...] = tuple(dict.fromkeys(REGISTRY_KIND_LOCATORS.values()))

_REGISTRY_KIND_ALLOWLIST_SQL = "kind IN ({values})".format(
    values=", ".join(f"'{kind}'" for kind in REGISTRY_KINDS)
)
_REGISTRY_KIND_LOCATOR_ALLOWLIST_SQL = "(kind, entity_table) IN ({pairs})".format(
    pairs=", ".join(f"('{kind}', '{table}')" for kind, table in REGISTRY_KIND_LOCATORS.items())
)


class EntityUuidRegistry(Base):
    """Sole ObjectRegistry: one UUIDv4 identity per registered concrete object.

    Each row binds exactly one UUIDv4 (`entity_id`, the single-column primary
    key) to one allowlisted object `kind` and its concrete typed-table locator
    (`entity_table`). The UUID alone resolves the row, so a caller never has to
    supply the object type to locate a registered object. This declaration is
    the only registry mapping in the schema; no parallel registry exists.
    """

    __tablename__ = "entity_uuid_registry"
    __table_args__ = (
        UniqueConstraint("entity_id", name="uq_entity_uuid_registry_entity_id"),
        CheckConstraint(
            _REGISTRY_KIND_ALLOWLIST_SQL,
            name="kind_allowlisted",
        ),
        CheckConstraint(
            _REGISTRY_KIND_LOCATOR_ALLOWLIST_SQL,
            name="kind_locator_allowlisted",
        ),
        Index("ix_entity_uuid_registry_entity_table", "entity_table"),
    )

    entity_id: Mapped[UUID] = mapped_column(UUID4, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_table: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


ASSERTION_KINDS: tuple[str, ...] = (
    "payload",
    "interval_boundary",
    "supersession",
    "deletion",
    "restoration",
)
"""Append-only evidence kinds a temporal assertion row may record."""

_ASSERTION_KIND_ALLOWLIST_SQL = "assertion_kind IN ({values})".format(
    values=", ".join(f"'{kind}'" for kind in ASSERTION_KINDS)
)


class TemporalAssertion(Base):
    """Sole registered version identity for one typed payload claim about one object.

    Each row is one assertion: a single UUIDv4 identity (`assertion_id`) that
    directly names, through `object_id`, the registered object whose typed
    payload it asserts. `object_id` references the ObjectRegistry
    (`EntityUuidRegistry`) rather than any typed table, so the assertion reuses
    the one registry boundary and no parallel version or object registry is
    introduced.

    Bitemporality:

    * effective time is the half-open interval ``[effective_from,
      effective_until)``; a NULL `effective_until` means open-ended;
    * transaction time is `recorded_at`, the immutable instant at which the
      assertion became known, together with its acceptance provenance.

    History is append-only. A correction, an interval boundary, a deletion or a
    restoration is a *new* row whose `supersedes_assertion_id` names the
    assertion it replaces; no accepted row is ever rewritten, so the known-time
    end of an assertion is derived from the `recorded_at` of the assertion that
    supersedes it and is deliberately not stored on the superseded row. Several
    rows may supersede one original, which is what a partial-interval correction
    (left copy, corrected overlap, right copy) appends. `revision_no` carries the
    monotonic accepted revision order per registered object, determined by
    accepted assertion order and not by any mutable current-state pointer.

    Domain payloads are not embedded here: `payload_family` names the
    family-specific payload table that is keyed by this assertion UUID. This
    declaration owns persistence shape only; resolution, supersession, deletion
    and ambiguity rejection belong to the services above it, and
    `SemanticChunkVersion` keeps its own current-pointer mechanism untouched.
    """

    __tablename__ = "temporal_assertions"
    __table_args__ = (
        UniqueConstraint("object_id", "revision_no", name="uq_temporal_assertions_object_revision"),
        CheckConstraint("revision_no > 0", name="revision_positive"),
        CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="effective_interval_half_open",
        ),
        CheckConstraint(_ASSERTION_KIND_ALLOWLIST_SQL, name="assertion_kind_allowlisted"),
        CheckConstraint("payload_family <> ''", name="payload_family_present"),
        CheckConstraint(
            "supersedes_assertion_id IS NULL OR supersedes_assertion_id <> assertion_id",
            name="supersedes_another_assertion",
        ),
        Index(
            "ix_temporal_assertions_object_effective",
            "object_id",
            "effective_from",
            "effective_until",
        ),
        Index("ix_temporal_assertions_object_recorded", "object_id", "recorded_at"),
        Index("ix_temporal_assertions_supersedes", "supersedes_assertion_id"),
        Index("ix_temporal_assertions_payload_family", "payload_family"),
    )

    assertion_id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    object_id: Mapped[UUID] = mapped_column(
        UUID4,
        ForeignKey("entity_uuid_registry.entity_id", ondelete="CASCADE"),
        nullable=False,
    )
    payload_family: Mapped[str] = mapped_column(String(64), nullable=False)
    assertion_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="payload", server_default="payload"
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_assertion_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("temporal_assertions.assertion_id", ondelete="SET NULL")
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    accepted_by: Mapped[str | None] = mapped_column(String(255))
    accepted_via: Mapped[str | None] = mapped_column(String(64))
    acceptance_operation_id: Mapped[UUID | None] = mapped_column(UUID4)
    acceptance_comment: Mapped[str | None] = mapped_column(Text)


class ContentAttachmentState(Base):
    """Whether a ContentAttachment was active, said as one assertion payload.

    This is the family payload table of `TemporalAssertion` for the
    `content_attachment` family, keyed by the assertion UUID it hangs from. The
    ORM mirrors the DDL migration `0018b_content_attachment_temporal_state`
    materializes; the constraint names come from the module naming convention
    and are byte-for-byte the ones that migration emits.

    It deliberately holds no identity of its own, no revision counter, no
    effective interval, no `recorded_at` or acceptance provenance, no
    current-state pointer and no lifecycle columns: every temporal property of
    an attachment state already belongs to the `temporal_assertions` row this
    payload keys, and duplicating any of it here would be the second version
    system the one-version rule forbids. The payload adds exactly one fact the
    shared assertion cannot express — was the attachment active over that
    interval — so creation, deactivation and restoration are all appended
    assertions carrying their own state row rather than rewrites of this one.
    """

    __tablename__ = "content_attachment_states"

    assertion_id: Mapped[UUID] = mapped_column(
        UUID4,
        ForeignKey("temporal_assertions.assertion_id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AttachableObjectMixin:
    """The exclusive endpoint role of the cross-content attachment graph.

    A mapped class mixes this in to declare that its rows may stand at either
    end of a directed ContentAttachment. `attachable_kind` is the object's
    existing allowlisted registry kind from `REGISTRY_KIND_LOCATORS`, so an
    endpoint is named by the one ObjectRegistry vocabulary and no parallel kind
    vocabulary is introduced. The role is the declaration alone: the edge table,
    its duplicate-edge and cycle rejection, and the temporal activity of an
    attachment stay outside this mixin, the latter already being the appended
    `TemporalAssertion` whose payload is `ContentAttachmentState`.
    """

    attachable_kind: ClassVar[str]


class Project(EntityCRUDMixin, Base):
    """A first-class project grouping documents under one UUID identity."""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("name", name="uq_projects_name"),
        Index("ix_projects_lifecycle", "is_deleted", "deleted_at"),
    )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(UUID4)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class File(AttachableObjectMixin, EntityCRUDMixin, Base):
    """A physical text file that may own, or be owned by, documents.

    `primary_source_id` is the exactly-one immutable source association: it is
    required, so no File exists without an original, and unique, so no two Files
    claim the same original. `ondelete="RESTRICT"` keeps the original
    undeletable while a File still depends on it. The association lives here
    rather than as a back-reference on `primary_sources` because only a required
    column on this side can make "exactly one" unrepresentable to violate; the
    reverse column would express "at most one" and would leave the two tables
    mutually referencing each other.

    The file's own bytes, archive placement and lifecycle columns are untouched
    by this association, and the class carries no attachment edges: mixing in
    `AttachableObjectMixin` declares the endpoint role only.
    """

    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("primary_source_id", name="uq_files_primary_source_id"),
        Index("ix_files_owner_id", "owner_id"),
        Index("ix_files_body_sha256", "body_sha256"),
        Index("ix_files_lifecycle", "is_deleted", "deleted_at"),
    )

    attachable_kind: ClassVar[str] = "file"

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(UUID4)
    primary_source_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("primary_sources.id", ondelete="RESTRICT"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(255))
    byte_length: Mapped[int | None] = mapped_column(BigInteger)
    char_count: Mapped[int | None] = mapped_column(BigInteger)
    checksum_algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="sha256")
    content_sha256: Mapped[str | None] = mapped_column(String(128))
    body_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    needs_revectorize: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    needs_rechunk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    block_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    primary_source: Mapped[PrimarySource] = relationship(back_populates="file")


class Document(AttachableObjectMixin, EntityCRUDMixin, Base):
    """A versioned source document and its ordered structural children.

    Mixing in `AttachableObjectMixin` declares the endpoint role only; the
    document's canonical hierarchy, publication boundary and shared
    id/name/owner identity are unchanged, and it keeps reaching its immutable
    original through the `ProcessingRun` that ingested it.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_upload_id", "source_version", name="uq_documents_upload_version"),
        CheckConstraint("source_version > 0", name="documents_source_version_positive"),
        Index("ix_documents_source_hash", "source_hash"),
        Index("ix_documents_body_sha256", "body_sha256"),
        Index("ix_documents_lifecycle", "processing_status", "deleted_at"),
    )

    attachable_kind: ClassVar[str] = "document"

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    owner_id: Mapped[UUID | None] = mapped_column(UUID4)
    source_upload_id: Mapped[UUID] = mapped_column(UUID4, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_path: Mapped[str | None] = mapped_column(String(2048))
    source_name: Mapped[str | None] = mapped_column(String(512))
    source_hash: Mapped[str | None] = mapped_column(String(128))
    checksum_algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="sha256")
    content_sha256: Mapped[str | None] = mapped_column(String(128))
    body_sha256: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    processing_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    needs_revectorize: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    processing_trace_id: Mapped[UUID | None] = mapped_column(UUID4)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    block_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    chapters: Mapped[list[Chapter]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="Chapter.order_index"
    )
    paragraphs: Mapped[list[Paragraph]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    semantic_chunks: Mapped[list[SemanticChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


ATTACHABLE_OBJECT_KINDS: tuple[str, ...] = (File.attachable_kind, Document.attachable_kind)
"""Registry kinds declared here as permitted AttachableObject endpoints.

Derived from the mapped classes that mix in `AttachableObjectMixin`, so the
allowlist cannot drift from the declarations. Project is the remaining endpoint
kind of the attachment graph and is added by the step that establishes its
endpoint behaviour; nothing here consumes this tuple as a closed set.
"""

ATTACHABLE_OBJECT_KIND_LOCATORS: Mapping[str, str] = MappingProxyType(
    {kind: REGISTRY_KIND_LOCATORS[kind] for kind in ATTACHABLE_OBJECT_KINDS}
)
"""Declared endpoint kinds paired with their existing concrete-table locators."""


class Chapter(EntityCRUDMixin, Base):
    """An ordered structural section belonging to one document."""

    __tablename__ = "chapters"
    __table_args__ = (
        UniqueConstraint("document_id", "order_index", name="uq_chapters_document_order"),
        CheckConstraint("order_index >= 0", name="chapters_order_nonnegative"),
        CheckConstraint(
            "source_start >= 0 AND source_end >= source_start", name="chapters_source_range_valid"
        ),
        Index("ix_chapters_document_order", "document_id", "order_index"),
    )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    document_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String(1024))
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_start: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_end: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    block_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    document: Mapped[Document] = relationship(back_populates="chapters")
    paragraphs: Mapped[list[Paragraph]] = relationship(
        back_populates="chapter", cascade="all, delete-orphan", order_by="Paragraph.order_index"
    )
    semantic_chunks: Mapped[list[SemanticChunk]] = relationship(
        back_populates="chapter", cascade="all, delete-orphan"
    )


class Paragraph(EntityCRUDMixin, Base):
    """An ordered, searchable text block belonging to one chapter."""

    __tablename__ = "paragraphs"
    __table_args__ = (
        UniqueConstraint("chapter_id", "order_index", name="uq_paragraphs_chapter_order"),
        CheckConstraint("order_index >= 0", name="paragraphs_order_nonnegative"),
        CheckConstraint(
            "source_start >= 0 AND source_end >= source_start", name="paragraphs_source_range_valid"
        ),
        Index("ix_paragraphs_chapter_order", "chapter_id", "order_index"),
        Index("ix_paragraphs_document_order", "document_id", "order_index"),
        Index("ix_paragraphs_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    document_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    source_start: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_end: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quality_score: Mapped[float | None]
    search_weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    block_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    document: Mapped[Document] = relationship(back_populates="paragraphs")
    chapter: Mapped[Chapter] = relationship(back_populates="paragraphs")
    semantic_chunks: Mapped[list[SemanticChunk]] = relationship(
        back_populates="paragraph", cascade="all, delete-orphan", order_by="SemanticChunk.order_index"
    )


class SemanticChunk(EntityCRUDMixin, Base):
    """A independently identified searchable chunk projected from a paragraph."""

    __tablename__ = "semantic_chunks"
    __table_args__ = (
        UniqueConstraint("paragraph_id", "order_index", name="uq_semantic_chunks_paragraph_order"),
        CheckConstraint("order_index >= 0", name="semantic_chunks_order_nonnegative"),
        CheckConstraint(
            "source_start >= 0 AND source_end >= source_start",
            name="semantic_chunks_source_range_valid",
        ),
        CheckConstraint("char_count >= 0", name="semantic_chunks_char_count_nonnegative"),
        Index("ix_semantic_chunks_document_order", "document_id", "order_index"),
        Index("ix_semantic_chunks_paragraph_order", "paragraph_id", "order_index"),
        Index("ix_semantic_chunks_chapter_order", "chapter_id", "order_index"),
        Index("ix_semantic_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    document_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    paragraph_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("paragraphs.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_start: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_end: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_type: Mapped[str | None] = mapped_column(String(64))
    chunk_type_id: Mapped[UUID | None] = mapped_column(UUID4, ForeignKey("chunk_types.id"))
    role_id: Mapped[UUID | None] = mapped_column(UUID4, ForeignKey("chunk_roles.id"))
    status_id: Mapped[UUID | None] = mapped_column(UUID4, ForeignKey("chunk_statuses.id"))
    block_type_id: Mapped[UUID | None] = mapped_column(UUID4, ForeignKey("block_types.id"))
    language_id: Mapped[UUID | None] = mapped_column(UUID4, ForeignKey("languages.id"))
    category_id: Mapped[UUID | None] = mapped_column(UUID4, ForeignKey("categories.id"))
    score: Mapped[float | None]
    search_weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR)
    block_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[Document] = relationship(back_populates="semantic_chunks")
    paragraph: Mapped[Paragraph] = relationship(back_populates="semantic_chunks")
    chapter: Mapped[Chapter] = relationship(back_populates="semantic_chunks")
    text_payload: Mapped[SemanticChunkText | None] = relationship(
        back_populates="chunk", cascade="all, delete-orphan", uselist=False
    )
    text_versions: Mapped[list[SemanticChunkVersion]] = relationship(
        back_populates="chunk", cascade="all, delete-orphan", order_by="SemanticChunkVersion.version_no"
    )
    current_text: Mapped[SemanticChunkCurrent | None] = relationship(
        back_populates="chunk", cascade="all, delete-orphan", uselist=False
    )


class SemanticChunkText(Base):
    """Full text payload for a semantic chunk, separated from structural metadata."""

    __tablename__ = "semantic_chunk_texts"
    __table_args__ = (
        CheckConstraint("char_count >= 0", name="semantic_chunk_texts_char_count_nonnegative"),
        Index("ix_semantic_chunk_texts_text_sha256", "text_sha256"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    block_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    chunk: Mapped[SemanticChunk] = relationship(back_populates="text_payload")


class SemanticChunkVersion(Base):
    """Immutable text history for a semantic chunk, keyed by version number."""

    __tablename__ = "semantic_chunk_versions"
    __table_args__ = (
        UniqueConstraint("chunk_uuid", "version_no", name="uq_semantic_chunk_versions_chunk_version"),
        UniqueConstraint("logical_chunk_id", "version_no", name="uq_semantic_chunk_versions_logical_version"),
        CheckConstraint("version_no > 0", name="semantic_chunk_versions_version_positive"),
        CheckConstraint("char_count >= 0", name="semantic_chunk_versions_char_count_nonnegative"),
        CheckConstraint(
            "status IN ('active', 'retired', 'deleted')",
            name="semantic_chunk_versions_status_valid",
        ),
        Index("ix_semantic_chunk_versions_chunk_version", "chunk_uuid", "version_no"),
        Index("ix_semantic_chunk_versions_logical_status_version", "logical_chunk_id", "status", "version_no"),
        Index("ix_semantic_chunk_versions_text_sha256", "text_sha256"),
        Index("ix_semantic_chunk_versions_status", "status"),
        Index("ix_semantic_chunk_versions_previous", "previous_version_id"),
        Index("ix_semantic_chunk_versions_operation", "operation_id"),
        Index("ix_semantic_chunk_versions_source", "source_version_id"),
        Index(
            "uq_semantic_chunk_versions_current_logical",
            "logical_chunk_id",
            unique=True,
            postgresql_where="is_current IS TRUE AND deleted_at IS NULL",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", ondelete="CASCADE"), nullable=False
    )
    logical_chunk_id: Mapped[UUID] = mapped_column(UUID4, nullable=False)
    previous_version_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("semantic_chunk_versions.id", ondelete="SET NULL")
    )
    restored_from_version_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("semantic_chunk_versions.id", ondelete="SET NULL")
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_version_id: Mapped[str | None] = mapped_column(String(255))
    source_start: Mapped[int | None] = mapped_column(BigInteger)
    source_end: Mapped[int | None] = mapped_column(BigInteger)
    order_index: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(255))
    operation: Mapped[str | None] = mapped_column(String(64))
    operation_id: Mapped[UUID | None] = mapped_column(UUID4)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    block_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    chunk: Mapped[SemanticChunk] = relationship(back_populates="text_versions")
    current: Mapped[SemanticChunkCurrent | None] = relationship(
        back_populates="version", uselist=False, viewonly=True
    )


class SemanticChunkCurrent(Base):
    """Selected active version pointer for a semantic chunk."""

    __tablename__ = "semantic_chunk_current"
    __table_args__ = (
        UniqueConstraint("version_id", name="uq_semantic_chunk_current_version_id"),
        Index("ix_semantic_chunk_current_version_id", "version_id"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    version_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunk_versions.id", ondelete="CASCADE"), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(255))
    operation: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chunk: Mapped[SemanticChunk] = relationship(back_populates="current_text")
    version: Mapped[SemanticChunkVersion] = relationship(
        back_populates="current", uselist=False
    )


class SemanticChunkTypeAssignment(Base):
    """Chunk-owned normalized assignment for the adapter SemanticChunk.type value."""

    __tablename__ = "semantic_chunk_type_assignments"
    __table_args__ = (
        Index("ix_semantic_chunk_type_assignments_chunk_type_id", "chunk_type_id"),
        Index("ix_semantic_chunk_type_assignments_chunk_version_id", "chunk_version_id"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scta_chunk", ondelete="CASCADE"), primary_key=True
    )
    chunk_version_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("semantic_chunk_versions.id", ondelete="SET NULL")
    )
    chunk_type_id: Mapped[UUID] = mapped_column(UUID4, ForeignKey("chunk_types.id", name="fk_scta_type"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chunk: Mapped[SemanticChunk] = relationship()
    chunk_type: Mapped[ChunkTypeDictionary] = relationship()


class SemanticChunkRoleAssignment(Base):
    """Chunk-owned normalized assignment for the adapter SemanticChunk.role value."""

    __tablename__ = "semantic_chunk_role_assignments"
    __table_args__ = (
        Index("ix_semantic_chunk_role_assignments_role_id", "role_id"),
        Index("ix_semantic_chunk_role_assignments_chunk_version_id", "chunk_version_id"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scra_chunk", ondelete="CASCADE"), primary_key=True
    )
    chunk_version_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("semantic_chunk_versions.id", ondelete="SET NULL")
    )
    role_id: Mapped[UUID] = mapped_column(UUID4, ForeignKey("chunk_roles.id", name="fk_scra_role"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chunk: Mapped[SemanticChunk] = relationship()
    role: Mapped[ChunkRoleDictionary] = relationship()


class SemanticChunkStatusAssignment(Base):
    """Chunk-owned normalized assignment for the adapter SemanticChunk.status value."""

    __tablename__ = "semantic_chunk_status_assignments"
    __table_args__ = (
        Index("ix_semantic_chunk_status_assignments_status_id", "status_id"),
        Index("ix_semantic_chunk_status_assignments_chunk_version_id", "chunk_version_id"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scsa_chunk", ondelete="CASCADE"), primary_key=True
    )
    chunk_version_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("semantic_chunk_versions.id", ondelete="SET NULL")
    )
    status_id: Mapped[UUID] = mapped_column(UUID4, ForeignKey("chunk_statuses.id", name="fk_scsa_status"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chunk: Mapped[SemanticChunk] = relationship()
    status: Mapped[ChunkStatusDictionary] = relationship()


class SemanticChunkBlockTypeAssignment(Base):
    """Chunk-owned normalized assignment for the adapter SemanticChunk.block_type value."""

    __tablename__ = "semantic_chunk_block_type_assignments"
    __table_args__ = (
        Index("ix_semantic_chunk_block_type_assignments_block_type_id", "block_type_id"),
        Index("ix_semantic_chunk_block_type_assignments_chunk_version_id", "chunk_version_id"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scbta_chunk", ondelete="CASCADE"), primary_key=True
    )
    chunk_version_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("semantic_chunk_versions.id", ondelete="SET NULL")
    )
    block_type_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("block_types.id", name="fk_scbta_block_type"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chunk: Mapped[SemanticChunk] = relationship()
    block_type: Mapped[BlockTypeDictionary] = relationship()


class SemanticChunkLanguageAssignment(Base):
    """Chunk-owned normalized assignment for the adapter SemanticChunk.language value."""

    __tablename__ = "semantic_chunk_language_assignments"
    __table_args__ = (
        Index("ix_semantic_chunk_language_assignments_language_id", "language_id"),
        Index("ix_semantic_chunk_language_assignments_chunk_version_id", "chunk_version_id"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scla_chunk", ondelete="CASCADE"), primary_key=True
    )
    chunk_version_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("semantic_chunk_versions.id", ondelete="SET NULL")
    )
    language_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("languages.id", name="fk_scla_language"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chunk: Mapped[SemanticChunk] = relationship()
    language: Mapped[LanguageDictionary] = relationship()


class SemanticChunkCategoryAssignment(Base):
    """Chunk-owned normalized assignment for the SemanticChunk.category value."""

    __tablename__ = "semantic_chunk_category_assignments"
    __table_args__ = (
        Index("ix_semantic_chunk_category_assignments_category_id", "category_id"),
        Index("ix_semantic_chunk_category_assignments_chunk_version_id", "chunk_version_id"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scca_chunk", ondelete="CASCADE"), primary_key=True
    )
    chunk_version_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("semantic_chunk_versions.id", ondelete="SET NULL")
    )
    category_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("categories.id", name="fk_scca_category"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chunk: Mapped[SemanticChunk] = relationship()
    category: Mapped[CategoryDictionary] = relationship()


SOURCE_STATE_PAYLOAD_FAMILY = "source_state"
"""`TemporalAssertion.payload_family` value whose payload table is `source_states`."""

PROCESSING_RUN_KINDS: tuple[str, ...] = (
    "document_create",
    "document_update",
    "file_ingest",
    "integrity_recheck",
)
"""Ingestion paths and recheck kinds one immutable run may execute."""

PROCESSING_RUN_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "aborted",
)
"""Terminal and in-flight states an auditable run may report."""

INTEGRITY_OUTCOMES: tuple[str, ...] = (
    "match",
    "mismatch",
    "length_boundary",
)
"""Round-trip verdicts: equal hashes, a first differing byte, or a strict prefix."""

_PROCESSING_RUN_KIND_ALLOWLIST_SQL = "run_kind IN ({values})".format(
    values=", ".join(f"'{kind}'" for kind in PROCESSING_RUN_KINDS)
)
_PROCESSING_RUN_STATUS_ALLOWLIST_SQL = "status IN ({values})".format(
    values=", ".join(f"'{status}'" for status in PROCESSING_RUN_STATUSES)
)
_INTEGRITY_OUTCOME_ALLOWLIST_SQL = "integrity_outcome IS NULL OR integrity_outcome IN ({values})".format(
    values=", ".join(f"'{outcome}'" for outcome in INTEGRITY_OUTCOMES)
)


class PrimarySource(EntityCRUDMixin, Base):
    """Immutable original material: the authoritative bytes behind everything derived.

    A row pins one original input by its byte locator and its original-byte
    SHA-256, together with the encoding as recorded at capture time, the media
    type and the free-form capture provenance. The hash is of the *original*
    bytes, never of a normalized rendering, so normalization can never replace,
    rewrite or weaken original-byte preservation: a normalized hash lives on the
    `ProcessingRun` that computed it and on the `SourceState` it produced.

    The row is immutable by construction, which is why it carries neither the
    `updated_at`/`is_deleted`/`deleted_at` lifecycle triple of the mutable
    content tables nor any revision counter: a correction is a new original with
    a new UUID, and a change of *state* is an appended `TemporalAssertion`
    carrying a `SourceState` payload, not an edit here.

    The File association is declared on `files.primary_source_id`, which is both
    required and unique: exactly one immutable original per File, and no sharing
    of one original between Files. It is held on that side alone so the
    cardinality is a NOT NULL constraint rather than a convention, and so the
    two tables do not reference each other in a cycle; `file` here is only the
    reverse view of that single foreign key. A `Document` reaches its original
    through the `ProcessingRun` that ingested it, which is the evidence chain
    PrimarySource -> ProcessingRun -> Document.
    """

    __tablename__ = "primary_sources"
    __table_args__ = (
        CheckConstraint("original_sha256 <> ''", name="original_hash_present"),
        CheckConstraint("source_locator <> ''", name="source_locator_present"),
        CheckConstraint("recorded_encoding <> ''", name="recorded_encoding_present"),
        CheckConstraint("byte_length IS NULL OR byte_length >= 0", name="byte_length_nonnegative"),
        Index("ix_primary_sources_original_sha256", "original_sha256"),
    )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    source_locator: Mapped[str] = mapped_column(String(2048), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(255))
    recorded_encoding: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_length: Mapped[int | None] = mapped_column(BigInteger)
    checksum_algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="sha256", server_default="sha256"
    )
    original_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    file: Mapped[File | None] = relationship(back_populates="primary_source")
    processing_runs: Mapped[list[ProcessingRun]] = relationship(back_populates="primary_source")
    source_states: Mapped[list[SourceState]] = relationship(back_populates="primary_source")


class ProcessingRun(EntityCRUDMixin, Base):
    """One immutable auditable execution over exactly one recorded comparison scope.

    A run is the evidence that a derived object exists lawfully: it names its
    input `PrimarySource`, the deterministic configuration that produced the
    outputs (`configuration_hash` plus the immutable normalization profile ID,
    version and complete parameter set), the actor, the status and the start and
    completion instants, and the typed object references — document, chapter and
    chunk — the execution produced or rechecked.

    Round-trip integrity is declared here rather than left to callers.
    `source_sha256` is the hash of the normalized Markdown or LaTeX source and
    `reconstruction_sha256` the hash of the same normalized scope as rebuilt by
    reconstruction; `run_succeeds_only_on_equal_hashes` makes a `succeeded`
    status unrepresentable unless both are present, equal and the outcome is
    `match`. A non-match must carry its controlled diagnostic instead: both
    hashes, and either the first differing byte offset or the strict-prefix
    length boundary, alongside the authorization decision and redaction policy
    under which any context may be released.

    `mismatch_span_id` names the SourceSpan diagnostic the run produced. It is
    deliberately an unconstrained UUID: the span table belongs to a later step of
    this branch, and a foreign key is added when that table exists rather than
    forward-declaring one here.

    Like `PrimarySource` the row is immutable once terminal and therefore carries
    no lifecycle triple and no revision counter; `operation_id` and
    `processing_trace_id` reuse the existing runtime correlation boundaries
    instead of introducing new ones.
    """

    __tablename__ = "processing_runs"
    __table_args__ = (
        CheckConstraint(_PROCESSING_RUN_KIND_ALLOWLIST_SQL, name="run_kind_allowlisted"),
        CheckConstraint(_PROCESSING_RUN_STATUS_ALLOWLIST_SQL, name="status_allowlisted"),
        CheckConstraint(_INTEGRITY_OUTCOME_ALLOWLIST_SQL, name="integrity_outcome_allowlisted"),
        CheckConstraint("actor <> ''", name="actor_present"),
        CheckConstraint("comparison_scope <> ''", name="comparison_scope_present"),
        CheckConstraint("configuration_hash <> ''", name="configuration_hash_present"),
        CheckConstraint(
            "normalization_profile_id <> '' AND normalization_profile_version <> ''",
            name="normalization_profile_identified",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR ("
            "integrity_outcome = 'match'"
            " AND source_sha256 IS NOT NULL"
            " AND reconstruction_sha256 IS NOT NULL"
            " AND source_sha256 = reconstruction_sha256)",
            name="run_succeeds_only_on_equal_hashes",
        ),
        CheckConstraint(
            "integrity_outcome IS NULL OR integrity_outcome = 'match' OR ("
            "source_sha256 IS NOT NULL"
            " AND reconstruction_sha256 IS NOT NULL"
            " AND (first_difference_offset IS NOT NULL OR mismatch_span_id IS NOT NULL))",
            name="non_match_carries_difference_evidence",
        ),
        CheckConstraint(
            "first_difference_offset IS NULL OR first_difference_offset >= 0",
            name="first_difference_offset_nonnegative",
        ),
        CheckConstraint(
            "completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)",
            name="run_interval_ordered",
        ),
        Index("ix_processing_runs_primary_source", "primary_source_id", "created_at"),
        Index("ix_processing_runs_document", "document_id", "created_at"),
        Index("ix_processing_runs_status", "status", "created_at"),
        Index("ix_processing_runs_configuration_hash", "configuration_hash"),
        Index("ix_processing_runs_operation_id", "operation_id"),
    )

    run_id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    primary_source_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("primary_sources.id", ondelete="RESTRICT"), nullable=False
    )
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    comparison_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    configuration_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    normalization_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    normalization_profile_version: Mapped[str] = mapped_column(String(64), nullable=False)
    normalization_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    source_encoding: Mapped[str | None] = mapped_column(String(64))
    reconstructed_encoding: Mapped[str | None] = mapped_column(String(64))
    checksum_algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="sha256", server_default="sha256"
    )
    source_sha256: Mapped[str | None] = mapped_column(String(128))
    reconstruction_sha256: Mapped[str | None] = mapped_column(String(128))
    integrity_outcome: Mapped[str | None] = mapped_column(String(32))
    first_difference_offset: Mapped[int | None] = mapped_column(BigInteger)
    mismatch_span_id: Mapped[UUID | None] = mapped_column(UUID4)
    diagnostic_authorization: Mapped[str | None] = mapped_column(String(32))
    redaction_policy: Mapped[str | None] = mapped_column(String(64))
    document_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("documents.id", ondelete="SET NULL")
    )
    chapter_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("chapters.id", ondelete="SET NULL")
    )
    chunk_id: Mapped[UUID | None] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", ondelete="SET NULL")
    )
    operation_id: Mapped[UUID | None] = mapped_column(UUID4)
    processing_trace_id: Mapped[UUID | None] = mapped_column(UUID4)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    primary_source: Mapped[PrimarySource] = relationship(back_populates="processing_runs")
    source_states: Mapped[list[SourceState]] = relationship(back_populates="produced_by_run")


class SourceState(Base):
    """A lossless restoration state of a PrimarySource, said as one assertion payload.

    This is the family payload table of `TemporalAssertion` for the
    `source_state` family, keyed by the assertion UUID it hangs from exactly as
    `ContentAttachmentState` is. That assertion UUID *is* the source-version
    identity a caller cites when asking for an as-of state, so this table needs
    no identity, no revision counter, no effective interval, no `recorded_at`,
    no acceptance provenance and no current-state pointer: every temporal
    property already belongs to the `temporal_assertions` row, and repeating any
    of it here would be the second version system the one-version rule forbids.
    The assertion's `object_id` names the registered content object whose source
    state this is, while `primary_source_id` pins the immutable original the
    state restores.

    The payload adds exactly the facts the shared assertion cannot express: a
    lossless reconstruction `manifest` or `reconstruction_locator` and the
    `restoration_sha256` that a restoration must reproduce, the reconstructed
    hash actually observed, the encoding the reconstruction retains, and the
    comparison scope it covers. Restoration therefore appends a new assertion
    carrying its own state row rather than rewriting this one.
    """

    __tablename__ = "source_states"
    __table_args__ = (
        CheckConstraint("restoration_sha256 <> ''", name="restoration_hash_present"),
        CheckConstraint("recorded_encoding <> ''", name="recorded_encoding_present"),
        CheckConstraint("comparison_scope <> ''", name="comparison_scope_present"),
        CheckConstraint(
            "manifest <> '{}'::jsonb OR reconstruction_locator IS NOT NULL",
            name="lossless_manifest_or_locator",
        ),
        Index("ix_source_states_primary_source", "primary_source_id"),
        Index("ix_source_states_produced_by_run", "produced_by_run_id"),
        Index("ix_source_states_restoration_sha256", "restoration_sha256"),
    )

    assertion_id: Mapped[UUID] = mapped_column(
        UUID4,
        ForeignKey("temporal_assertions.assertion_id", ondelete="CASCADE"),
        primary_key=True,
    )
    primary_source_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("primary_sources.id", ondelete="RESTRICT"), nullable=False
    )
    produced_by_run_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("processing_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    reconstruction_locator: Mapped[str | None] = mapped_column(String(2048))
    checksum_algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="sha256", server_default="sha256"
    )
    restoration_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    reconstructed_sha256: Mapped[str | None] = mapped_column(String(128))
    recorded_encoding: Mapped[str] = mapped_column(String(64), nullable=False)
    comparison_scope: Mapped[str] = mapped_column(String(64), nullable=False)

    primary_source: Mapped[PrimarySource] = relationship(back_populates="source_states")
    produced_by_run: Mapped[ProcessingRun] = relationship(back_populates="source_states")


class SourceSpan(Base):
    """One independently referenceable bounded diagnostic about one source region.

    This is the carrier a refused round trip needs. When the normalized source
    and the reconstructed scope disagree, ingestion is non-successful and the
    hierarchy it had written rolls back, so the failed `ProcessingRun` survives
    in a separate evidence transaction with its `document_id`, `chapter_id` and
    `chunk_id` necessarily NULL — that is exactly what those `SET NULL` foreign
    keys mean once their rows are gone. The Document, Chapter and SemanticChunk
    identifiers the controlled integrity error must still name are therefore
    carried here instead, which is why `document_id`, `chapter_id` and
    `chunk_id` are plain UUID columns with no foreign key: the span records what
    it diagnosed, not rows that must continue to exist. `produced_by_run_id` is
    the one real reference, and it is `RESTRICT` so a run cannot be deleted
    while a diagnostic still cites it. `source_state_assertion_id` is likewise
    unconstrained and nullable, because a refused publication accepted no
    `SourceState` for the span to hang from.

    The located region is said in whichever way the verdict supports: a
    character range, and then either the first differing `byte_offset` or the
    `length_boundary` at which one value proved a strict prefix of the other —
    never both, which `exactly_one_difference_locator` enforces. `page` and
    `bounding_box` add layout coordinates when the source has them, and
    `fragment_sha256` pins the exact fragment.

    Disclosure is not incidental. `context_before` and `context_after` may only
    be released under the `authorization_decision` made for the caller and the
    `redaction_policy` applied, with `redaction_applied` recording whether that
    policy actually altered what is stored; all three are required, so a span
    cannot exist without saying under what authority its context may be read.

    Like the sibling provenance tables the row is immutable: no `updated_at`,
    `is_deleted`, `deleted_at` or `revision_no`, because a correction is a new
    span with a new UUID, never an edit of this one. The table is deliberately
    absent from the entity UUID registry, exactly as `primary_sources` and
    `processing_runs` are: a diagnostic is evidence about registered objects,
    not itself a registered addressable object.
    """

    __tablename__ = "source_spans"
    __table_args__ = (
        CheckConstraint("authorization_decision <> ''", name="authorization_decision_present"),
        CheckConstraint("redaction_policy <> ''", name="redaction_policy_present"),
        CheckConstraint(
            "(byte_offset IS NULL) <> (length_boundary IS NULL)",
            name="exactly_one_difference_locator",
        ),
    )

    span_id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    produced_by_run_id: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("processing_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    source_state_assertion_id: Mapped[UUID | None] = mapped_column(UUID4)
    document_id: Mapped[UUID | None] = mapped_column(UUID4)
    chapter_id: Mapped[UUID | None] = mapped_column(UUID4)
    chunk_id: Mapped[UUID | None] = mapped_column(UUID4)
    character_start: Mapped[int | None] = mapped_column(BigInteger)
    character_end: Mapped[int | None] = mapped_column(BigInteger)
    byte_offset: Mapped[int | None] = mapped_column(BigInteger)
    length_boundary: Mapped[int | None] = mapped_column(BigInteger)
    context_before: Mapped[str | None] = mapped_column(Text)
    context_after: Mapped[str | None] = mapped_column(Text)
    authorization_decision: Mapped[str] = mapped_column(String(32), nullable=False)
    redaction_policy: Mapped[str] = mapped_column(String(64), nullable=False)
    redaction_applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer)
    bounding_box: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    fragment_sha256: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = (
    "ASSERTION_KINDS",
    "ATTACHABLE_OBJECT_KINDS",
    "ATTACHABLE_OBJECT_KIND_LOCATORS",
    "AttachableObjectMixin",
    "Base",
    "BlockTypeDictionary",
    "CategoryDictionary",
    "Chapter",
    "ChunkRoleDictionary",
    "ChunkStatusDictionary",
    "ChunkTypeDictionary",
    "Document",
    "DictionaryCRUDMixin",
    "EntityCRUDMixin",
    "EntityUuidRegistry",
    "File",
    "INTEGRITY_OUTCOMES",
    "LanguageDictionary",
    "PROCESSING_RUN_KINDS",
    "PROCESSING_RUN_STATUSES",
    "Paragraph",
    "PrimarySource",
    "ProcessingRun",
    "Project",
    "REGISTRY_KINDS",
    "REGISTRY_KIND_LOCATORS",
    "REGISTRY_TABLE_LOCATORS",
    "SOURCE_STATE_PAYLOAD_FAMILY",
    "SemanticChunk",
    "SemanticChunkBlockTypeAssignment",
    "SemanticChunkCategoryAssignment",
    "SemanticChunkLanguageAssignment",
    "SemanticChunkRoleAssignment",
    "SemanticChunkStatusAssignment",
    "SemanticChunkText",
    "SemanticChunkVersion",
    "SemanticChunkCurrent",
    "SemanticChunkTypeAssignment",
    "SourceSpan",
    "SourceState",
    "TemporalAssertion",
    "metadata",
)
