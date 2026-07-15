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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


class Document(EntityCRUDMixin, Base):
    """A versioned source document and its ordered structural children."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_upload_id", "source_version", name="uq_documents_upload_version"),
        CheckConstraint("source_version > 0", name="documents_source_version_positive"),
        Index("ix_documents_source_hash", "source_hash"),
        Index("ix_documents_lifecycle", "processing_status", "deleted_at"),
    )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    source_upload_id: Mapped[UUID] = mapped_column(UUID4, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_path: Mapped[str | None] = mapped_column(String(2048))
    source_name: Mapped[str | None] = mapped_column(String(512))
    source_hash: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    processing_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_start: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_end: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_type: Mapped[str | None] = mapped_column(String(64))
    score: Mapped[float | None]
    search_weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR)
    block_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[Document] = relationship(back_populates="semantic_chunks")
    paragraph: Mapped[Paragraph] = relationship(back_populates="semantic_chunks")
    chapter: Mapped[Chapter] = relationship(back_populates="semantic_chunks")


__all__ = (
    "Base",
    "Chapter",
    "Document",
    "EntityCRUDMixin",
    "EntityUuidRegistry",
    "Paragraph",
    "SemanticChunk",
    "metadata",
)
