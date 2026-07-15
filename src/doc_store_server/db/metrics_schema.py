"""Relational persistence contract for optional semantic-chunk metrics."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .schema import Base, SemanticChunk, UUID4


FieldAliases = Mapping[str, tuple[str, ...]]


# Each database column is authoritative.  Readers reconstruct nested metrics and
# compatibility aliases from this immutable contract without storing duplicates.
METRICS_FIELD_ALIASES: Final[FieldAliases] = MappingProxyType(
    {
        "quality_score": ("metrics.quality_score", "quality_score"),
        "coverage": ("metrics.coverage", "coverage"),
        "cohesion": ("metrics.cohesion", "cohesion"),
        "boundary_prev": ("metrics.boundary_prev", "boundary_prev"),
        "boundary_next": ("metrics.boundary_next", "boundary_next"),
        "matches": ("metrics.matches", "matches"),
        "used_in_generation": ("metrics.used_in_generation", "used_in_generation"),
        "used_as_input": ("metrics.used_as_input", "used_as_input"),
        "used_as_context": ("metrics.used_as_context", "used_as_context"),
    }
)

FEEDBACK_FIELD_ALIASES: Final[FieldAliases] = MappingProxyType(
    {
        "accepted": ("metrics.feedback.accepted", "feedback_accepted"),
        "rejected": ("metrics.feedback.rejected", "feedback_rejected"),
        "modifications": ("metrics.feedback.modifications", "feedback_modifications"),
    }
)


class SemanticChunkMetrics(Base):
    """Optional one-to-one metrics row belonging to a semantic chunk."""

    __tablename__ = "semantic_chunk_metrics"
    __table_args__ = (
        CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)",
            name="semantic_chunk_metrics_quality_score_range",
        ),
        CheckConstraint(
            "coverage IS NULL OR (coverage >= 0 AND coverage <= 1)",
            name="semantic_chunk_metrics_coverage_range",
        ),
        CheckConstraint(
            "cohesion IS NULL OR (cohesion >= 0 AND cohesion <= 1)",
            name="semantic_chunk_metrics_cohesion_range",
        ),
        CheckConstraint(
            "boundary_prev IS NULL OR (boundary_prev >= 0 AND boundary_prev <= 1)",
            name="semantic_chunk_metrics_boundary_prev_range",
        ),
        CheckConstraint(
            "boundary_next IS NULL OR (boundary_next >= 0 AND boundary_next <= 1)",
            name="semantic_chunk_metrics_boundary_next_range",
        ),
        CheckConstraint(
            "matches IS NULL OR matches >= 0", name="semantic_chunk_metrics_matches_nonnegative"
        ),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4,
        ForeignKey("semantic_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    quality_score: Mapped[float | None] = mapped_column(Float)
    coverage: Mapped[float | None] = mapped_column(Float)
    cohesion: Mapped[float | None] = mapped_column(Float)
    boundary_prev: Mapped[float | None] = mapped_column(Float)
    boundary_next: Mapped[float | None] = mapped_column(Float)
    matches: Mapped[int | None] = mapped_column(Integer)
    used_in_generation: Mapped[bool | None] = mapped_column(Boolean)
    used_as_input: Mapped[bool | None] = mapped_column(Boolean)
    used_as_context: Mapped[bool | None] = mapped_column(Boolean)

    semantic_chunk: Mapped[SemanticChunk] = relationship()
    feedback: Mapped[SemanticChunkFeedback | None] = relationship(
        back_populates="metrics", uselist=False, cascade="all, delete-orphan"
    )


class SemanticChunkFeedback(Base):
    """Optional one-to-one feedback row nested below chunk metrics."""

    __tablename__ = "semantic_chunk_feedback"
    __table_args__ = (
        CheckConstraint(
            "accepted IS NULL OR accepted >= 0",
            name="semantic_chunk_feedback_accepted_nonnegative",
        ),
        CheckConstraint(
            "rejected IS NULL OR rejected >= 0",
            name="semantic_chunk_feedback_rejected_nonnegative",
        ),
        CheckConstraint(
            "modifications IS NULL OR modifications >= 0",
            name="semantic_chunk_feedback_modifications_nonnegative",
        ),
    )

    chunk_uuid: Mapped[UUID] = mapped_column(
        UUID4,
        ForeignKey("semantic_chunk_metrics.chunk_uuid", ondelete="CASCADE"),
        primary_key=True,
    )
    accepted: Mapped[int | None] = mapped_column(Integer)
    rejected: Mapped[int | None] = mapped_column(Integer)
    modifications: Mapped[int | None] = mapped_column(Integer)

    metrics: Mapped[SemanticChunkMetrics] = relationship(back_populates="feedback")


__all__ = (
    "FEEDBACK_FIELD_ALIASES",
    "METRICS_FIELD_ALIASES",
    "SemanticChunkFeedback",
    "SemanticChunkMetrics",
)
