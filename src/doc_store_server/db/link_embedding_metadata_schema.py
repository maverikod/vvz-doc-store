"""Typed persistence contracts for chunk links, embeddings, and block metadata.

The root ``semantic_chunks`` relation is owned by :mod:`.schema`; this module
only projects its remaining child relations and pure compatibility helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Iterable, Mapping, Sequence, TypedDict
from uuid import UUID, uuid4

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .schema import Base, SemanticChunk, UUID4


FieldAliases = Mapping[str, tuple[str, ...]]


LINK_FIELD_ALIASES: Final[FieldAliases] = MappingProxyType(
    {
        "source_chunk_uuid": ("source_chunk_uuid", "links.source_chunk_uuid"),
        "relation_type": ("relation_type", "links.relation_type"),
        "target_chunk_uuid": ("target_chunk_uuid", "links.target_chunk_uuid"),
        "ordinal": ("ordinal", "links.ordinal"),
        "relation_data": ("relation_data", "links.relation_data"),
    }
)
LINK_COMPATIBILITY_ALIASES: Final[FieldAliases] = MappingProxyType(
    {
        "links": ("links",),
        "link_parent": ("link_parent",),
        "link_related": ("link_related",),
    }
)
EMBEDDING_FIELD_ALIASES: Final[FieldAliases] = MappingProxyType(
    {
        "embedding": ("vector",),
        "embedding_model": ("model",),
    }
)


class BlockMetaPromotion(TypedDict, total=False):
    """Known block metadata promoted for typed access; unknown keys stay JSONB."""

    parent_id: UUID | str
    parent_type: str
    source_start: int
    source_end: int
    markup: str
    list_level: int
    heading_level: int
    aggregation: dict[str, Any]


class SemanticChunkLinkRecord(TypedDict):
    """Minimal typed shape accepted by ordinal reconstruction."""

    source_chunk_uuid: UUID
    relation_type: str
    target_chunk_uuid: UUID
    ordinal: int


KNOWN_BLOCK_META_KEYS: Final[frozenset[str]] = frozenset(BlockMetaPromotion.__annotations__)


@dataclass(frozen=True, slots=True)
class BlockMetaParts:
    """Typed promotion plus untouched extension keys from ``block_meta``."""

    promoted: BlockMetaPromotion
    extensions: dict[str, Any]


def split_block_meta(value: Mapping[str, Any] | None) -> BlockMetaParts:
    """Split known metadata from extensions without dropping or mutating input."""

    source = dict(value or {})
    promoted: BlockMetaPromotion = {
        key: source[key] for key in KNOWN_BLOCK_META_KEYS if key in source  # type: ignore[typeddict-item]
    }
    extensions = {key: item for key, item in source.items() if key not in KNOWN_BLOCK_META_KEYS}
    return BlockMetaParts(promoted=promoted, extensions=extensions)


def merge_block_meta(
    promoted: Mapping[str, Any], extensions: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Recombine metadata with one authority per key and lossless extensions."""

    result = dict(extensions or {})
    result.update({key: value for key, value in promoted.items() if key in KNOWN_BLOCK_META_KEYS})
    return result


def promote_block_meta(value: Mapping[str, Any] | None) -> BlockMetaPromotion:
    """Return only the typed, known projection of a JSONB metadata mapping."""

    return split_block_meta(value).promoted


def reconstruct_link_ordinals(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return link records in deterministic source/type/ordinal/target order."""

    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            str(row.get("source_chunk_uuid", "")),
            str(row.get("relation_type", "")),
            int(row.get("ordinal", 0)),
            str(row.get("target_chunk_uuid", "")),
        ),
    )
    return tuple(ordered)


def select_active_embedding(
    rows: Sequence[Mapping[str, Any]], requested_model: str, requested_dimension: int
) -> Mapping[str, Any] | None:
    """Choose exactly one compatible active embedding, retaining all history."""

    compatible = [
        row
        for row in rows
        if row.get("active") is True
        and row.get("model") == requested_model
        and int(row.get("dimension", -1)) == requested_dimension
    ]
    if not compatible:
        return None
    return max(
        compatible,
        key=lambda row: (
            row.get("created_at") or datetime.min,
            str(row.get("id", row.get("row_uuid", ""))),
        ),
    )


class SemanticChunkLink(Base):
    """Ordered directed relation between two semantic chunks."""

    __tablename__ = "semantic_chunk_links"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="semantic_chunk_links_ordinal_nonnegative"),
        UniqueConstraint(
            "source_chunk_uuid",
            "relation_type",
            "target_chunk_uuid",
            "ordinal",
            name="uq_semantic_chunk_links_ordered_identity",
        ),
        Index("ix_semantic_chunk_links_source_type", "source_chunk_uuid", "relation_type"),
        Index("ix_semantic_chunk_links_target", "target_chunk_uuid"),
    )

    source_chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_chunk_uuid: Mapped[UUID] = mapped_column(UUID4, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    relation_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    source_chunk: Mapped[SemanticChunk] = relationship(foreign_keys=[source_chunk_uuid])


class SemanticChunkEmbedding(Base):
    """Versioned embedding child; inactive rows preserve embedding history."""

    __tablename__ = "semantic_chunk_embeddings"
    __table_args__ = (
        CheckConstraint("dimension > 0", name="semantic_chunk_embeddings_dimension_positive"),
        UniqueConstraint(
            "chunk_uuid",
            "model",
            "provider",
            "model_version",
            "dimension",
            name="uq_semantic_chunk_embeddings_version",
        ),
        Index("ix_semantic_chunk_embeddings_chunk_model", "chunk_uuid", "model", "dimension"),
        Index(
            "ix_semantic_chunk_embeddings_vector_cosine",
            "vector",
            postgresql_using="hnsw",
            postgresql_ops={"vector": "vector_cosine_ops"},
        ),
        Index(
            "uq_semantic_chunk_embeddings_active_compatibility",
            "chunk_uuid",
            "model",
            "dimension",
            unique=True,
            postgresql_where="active IS TRUE",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID4, primary_key=True, default=uuid4)
    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4, ForeignKey("semantic_chunks.id", ondelete="CASCADE"), nullable=False
    )
    vector: Mapped[list[float]] = mapped_column(VECTOR, nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    semantic_chunk: Mapped[SemanticChunk] = relationship()

    @property
    def embedding(self) -> list[float]:
        return self.vector

    @property
    def embedding_model(self) -> str:
        return self.model


__all__ = (
    "BlockMetaParts",
    "BlockMetaPromotion",
    "EMBEDDING_FIELD_ALIASES",
    "KNOWN_BLOCK_META_KEYS",
    "LINK_COMPATIBILITY_ALIASES",
    "LINK_FIELD_ALIASES",
    "SemanticChunkEmbedding",
    "SemanticChunkLinkRecord",
    "SemanticChunkLink",
    "merge_block_meta",
    "promote_block_meta",
    "reconstruct_link_ordinals",
    "select_active_embedding",
    "split_block_meta",
)
