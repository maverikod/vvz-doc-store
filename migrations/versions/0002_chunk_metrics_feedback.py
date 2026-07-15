"""Create optional semantic-chunk metrics and feedback tables."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_chunk_metrics_feedback"
down_revision = "0001_hierarchy_chunk_root"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create optional one-to-one metrics and feedback children."""
    op.create_table(
        "semantic_chunk_metrics",
        sa.Column("chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column("cohesion", sa.Float(), nullable=True),
        sa.Column("boundary_prev", sa.Float(), nullable=True),
        sa.Column("boundary_next", sa.Float(), nullable=True),
        sa.Column("matches", sa.Integer(), nullable=True),
        sa.Column("used_in_generation", sa.Boolean(), nullable=True),
        sa.Column("used_as_input", sa.Boolean(), nullable=True),
        sa.Column("used_as_context", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["chunk_uuid"],
            ["semantic_chunks.id"],
            name="fk_semantic_chunk_metrics_chunk_uuid_semantic_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_uuid", name="pk_semantic_chunk_metrics"),
        sa.CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)",
            name="semantic_chunk_metrics_quality_score_range",
        ),
        sa.CheckConstraint(
            "coverage IS NULL OR (coverage >= 0 AND coverage <= 1)",
            name="semantic_chunk_metrics_coverage_range",
        ),
        sa.CheckConstraint(
            "cohesion IS NULL OR (cohesion >= 0 AND cohesion <= 1)",
            name="semantic_chunk_metrics_cohesion_range",
        ),
        sa.CheckConstraint(
            "boundary_prev IS NULL OR (boundary_prev >= 0 AND boundary_prev <= 1)",
            name="semantic_chunk_metrics_boundary_prev_range",
        ),
        sa.CheckConstraint(
            "boundary_next IS NULL OR (boundary_next >= 0 AND boundary_next <= 1)",
            name="semantic_chunk_metrics_boundary_next_range",
        ),
        sa.CheckConstraint(
            "matches IS NULL OR matches >= 0",
            name="semantic_chunk_metrics_matches_nonnegative",
        ),
    )

    op.create_table(
        "semantic_chunk_feedback",
        sa.Column("chunk_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=True),
        sa.Column("rejected", sa.Integer(), nullable=True),
        sa.Column("modifications", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["chunk_uuid"],
            ["semantic_chunk_metrics.chunk_uuid"],
            name="fk_semantic_chunk_feedback_chunk_uuid_semantic_chunk_metrics",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_uuid", name="pk_semantic_chunk_feedback"),
        sa.CheckConstraint(
            "accepted IS NULL OR accepted >= 0",
            name="semantic_chunk_feedback_accepted_nonnegative",
        ),
        sa.CheckConstraint(
            "rejected IS NULL OR rejected >= 0",
            name="semantic_chunk_feedback_rejected_nonnegative",
        ),
        sa.CheckConstraint(
            "modifications IS NULL OR modifications >= 0",
            name="semantic_chunk_feedback_modifications_nonnegative",
        ),
    )


def downgrade() -> None:
    """Drop feedback before metrics, preserving the root schema."""
    op.drop_table("semantic_chunk_feedback")
    op.drop_table("semantic_chunk_metrics")
