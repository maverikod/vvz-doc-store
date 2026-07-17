"""PostgreSQL persistence mappings for the canonical document hierarchy."""

from __future__ import annotations

from datetime import datetime
from typing import Any
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


class EntityUuidRegistry(Base):
    """Global UUID registry for addressable entity rows."""

    __tablename__ = "entity_uuid_registry"
    __table_args__ = (
        UniqueConstraint("entity_id", name="uq_entity_uuid_registry_entity_id"),
        Index("ix_entity_uuid_registry_entity_table", "entity_table"),
    )

    entity_table: Mapped[str] = mapped_column(String(128), primary_key=True)
    entity_id: Mapped[UUID] = mapped_column(UUID4, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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


class File(EntityCRUDMixin, Base):
    """A physical text file that may own, or be owned by, documents."""

    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_owner_id", "owner_id"),
        Index("ix_files_body_sha256", "body_sha256"),
        Index("ix_files_lifecycle", "is_deleted", "deleted_at"),
    )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(UUID4)
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


class Document(EntityCRUDMixin, Base):
    """A versioned source document and its ordered structural children."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_upload_id", "source_version", name="uq_documents_upload_version"),
        CheckConstraint("source_version > 0", name="documents_source_version_positive"),
        Index("ix_documents_source_hash", "source_hash"),
        Index("ix_documents_body_sha256", "body_sha256"),
        Index("ix_documents_lifecycle", "processing_status", "deleted_at"),
    )

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


class SemanticChunkTypeAssignment(Base):
    """Chunk-owned normalized assignment for the adapter SemanticChunk.type value."""

    __tablename__ = "semantic_chunk_type_assignments"
    __table_args__ = (Index("ix_semantic_chunk_type_assignments_chunk_type_id", "chunk_type_id"),)

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scta_chunk", ondelete="CASCADE"), primary_key=True
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
    __table_args__ = (Index("ix_semantic_chunk_role_assignments_role_id", "role_id"),)

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scra_chunk", ondelete="CASCADE"), primary_key=True
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
    __table_args__ = (Index("ix_semantic_chunk_status_assignments_status_id", "status_id"),)

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scsa_chunk", ondelete="CASCADE"), primary_key=True
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
    __table_args__ = (Index("ix_semantic_chunk_block_type_assignments_block_type_id", "block_type_id"),)

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scbta_chunk", ondelete="CASCADE"), primary_key=True
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
    __table_args__ = (Index("ix_semantic_chunk_language_assignments_language_id", "language_id"),)

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scla_chunk", ondelete="CASCADE"), primary_key=True
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
    __table_args__ = (Index("ix_semantic_chunk_category_assignments_category_id", "category_id"),)

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", name="fk_scca_chunk", ondelete="CASCADE"), primary_key=True
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


__all__ = (
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
    "LanguageDictionary",
    "Paragraph",
    "Project",
    "SemanticChunk",
    "SemanticChunkBlockTypeAssignment",
    "SemanticChunkCategoryAssignment",
    "SemanticChunkLanguageAssignment",
    "SemanticChunkRoleAssignment",
    "SemanticChunkStatusAssignment",
    "SemanticChunkText",
    "SemanticChunkTypeAssignment",
    "metadata",
)
