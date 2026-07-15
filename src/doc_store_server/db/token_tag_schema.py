"""Normalized, ordered token and tag mappings for semantic chunks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .schema import Base, SemanticChunk, UUID4


TokenKind = Literal["tokens", "bm25_tokens"]
TOKEN_KINDS: Final[tuple[TokenKind, TokenKind]] = ("tokens", "bm25_tokens")
TagsFlat = str


class SemanticChunkToken(Base):
    """One canonical or BM25 token at a deterministic position in a chunk."""

    __tablename__ = "semantic_chunk_tokens"
    __table_args__ = (
        CheckConstraint(
            "token_kind IN ('tokens', 'bm25_tokens')",
            name="semantic_chunk_tokens_kind_valid",
        ),
        CheckConstraint("ordinal >= 0", name="semantic_chunk_tokens_ordinal_nonnegative"),
        UniqueConstraint(
            "chunk_uuid",
            "token_kind",
            "ordinal",
            name="uq_semantic_chunk_tokens_identity",
        ),
        Index("ix_semantic_chunk_tokens_chunk_kind_ordinal", "chunk_uuid", "token_kind", "ordinal"),
        Index("ix_semantic_chunk_tokens_kind_value", "token_kind", "token_value"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4,
        ForeignKey("semantic_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    token_kind: Mapped[TokenKind] = mapped_column(Text, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_value: Mapped[str] = mapped_column(Text, nullable=False)

    semantic_chunk: Mapped[SemanticChunk] = relationship()


class SemanticChunkTag(Base):
    """One canonical tag at a deterministic position in a chunk."""

    __tablename__ = "semantic_chunk_tags"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="semantic_chunk_tags_ordinal_nonnegative"),
        UniqueConstraint(
            "chunk_uuid", "ordinal", name="uq_semantic_chunk_tags_identity"
        ),
        Index("ix_semantic_chunk_tags_chunk_ordinal", "chunk_uuid", "ordinal"),
        Index("ix_semantic_chunk_tags_value", "tag_value"),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4,
        ForeignKey("semantic_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag_value: Mapped[str] = mapped_column(Text, nullable=False)

    semantic_chunk: Mapped[SemanticChunk] = relationship()


def reconstruct_token_groups(
    rows: Iterable[SemanticChunkToken],
) -> Mapping[TokenKind, tuple[str, ...]]:
    """Return each token kind in ordinal order without mutating the input."""

    grouped: dict[TokenKind, list[SemanticChunkToken]] = {kind: [] for kind in TOKEN_KINDS}
    for row in rows:
        grouped[row.token_kind].append(row)
    return {
        kind: tuple(row.token_value for row in sorted(grouped[kind], key=lambda item: item.ordinal))
        for kind in TOKEN_KINDS
    }


def reconstruct_tags(rows: Iterable[SemanticChunkTag]) -> tuple[str, ...]:
    """Return canonical tags in ordinal order without mutating the input."""

    return tuple(row.tag_value for row in sorted(rows, key=lambda item: item.ordinal))


def derive_tags_flat(tags: Iterable[str]) -> TagsFlat:
    """Derive the compatibility string only from the canonical ordered tags."""

    return ", ".join(tags)


def reconstruct_tags_flat(rows: Iterable[SemanticChunkTag]) -> TagsFlat:
    """Reconstruct the derived ``tags_flat`` value from normalized tag rows."""

    return derive_tags_flat(reconstruct_tags(rows))


__all__ = (
    "TOKEN_KINDS",
    "TagsFlat",
    "TokenKind",
    "SemanticChunkTag",
    "SemanticChunkToken",
    "derive_tags_flat",
    "reconstruct_tags",
    "reconstruct_tags_flat",
    "reconstruct_token_groups",
)
