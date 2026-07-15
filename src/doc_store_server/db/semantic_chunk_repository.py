"""Transactional persistence boundary for complete semantic-chunk aggregates."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .link_embedding_metadata_schema import (
    SemanticChunkEmbedding,
    SemanticChunkLink,
    split_block_meta,
)
from .metrics_schema import SemanticChunkFeedback, SemanticChunkMetrics
from .schema import SemanticChunk as SemanticChunkRow
from .semantic_chunk_mapper import (
    EmbeddingRow,
    FeedbackRow,
    LinkRow,
    MetricsRow,
    RootRow,
    SemanticChunkRows,
    TagRow,
    TokenRow,
    from_rows,
    to_rows,
)
from .token_tag_schema import SemanticChunkTag, SemanticChunkToken

if TYPE_CHECKING:
    from chunk_metadata_adapter import SemanticChunk


class SemanticChunkNotFoundError(LookupError):
    """Raised when a requested semantic chunk does not exist."""


class SemanticChunkRepository:
    """Own one async transaction for reading or replacing one chunk aggregate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        chunk: SemanticChunk,
        *,
        embedding_provider: str = "",
        embedding_model_version: str = "",
        embedding_created_at: datetime | None = None,
        embedding_active: bool = True,
    ) -> SemanticChunk:
        """Atomically upsert a mapped aggregate and return its canonical value."""
        rows = to_rows(
            chunk,
            embedding_provider=embedding_provider,
            embedding_model_version=embedding_model_version,
            embedding_created_at=embedding_created_at,
            embedding_active=embedding_active,
        )
        async with self._session.begin():
            root = await self._lock_root(rows.root["id"])
            if root is None:
                root = SemanticChunkRow(**dict(rows.root))
                self._session.add(root)
            else:
                for key, value in rows.root.items():
                    setattr(root, key, value)
            await self._session.flush()

            await self._replace_metrics(rows.root["id"], rows.metrics, rows.feedback)
            await self._replace_ordered_children(rows)
            await self._upsert_embeddings(rows.embeddings)
            await self._session.flush()
            stored = await self._read_rows(root)

        return from_rows(
            stored,
            requested_model=rows.embeddings[0]["model"] if rows.embeddings else None,
            requested_dimension=rows.embeddings[0]["dimension"] if rows.embeddings else None,
        )

    async def save(self, chunk: SemanticChunk, **kwargs: Any) -> SemanticChunk:
        """Compatibility spelling for :meth:`upsert`."""
        return await self.upsert(chunk, **kwargs)

    async def get_by_uuid(
        self,
        chunk_uuid: UUID,
        *,
        requested_model: str | None = None,
        requested_dimension: int | None = None,
    ) -> SemanticChunk:
        """Read and reconstruct one aggregate, or raise an explicit not-found error."""
        async with self._session.begin():
            root = (
                await self._session.execute(
                    select(SemanticChunkRow).where(SemanticChunkRow.id == chunk_uuid)
                )
            ).scalar_one_or_none()
            if root is None:
                raise SemanticChunkNotFoundError(f"semantic chunk not found: {chunk_uuid}")

            rows = await self._read_rows(root)
            return from_rows(
                rows,
                requested_model=requested_model,
                requested_dimension=requested_dimension,
            )

    async def read_by_uuid(self, chunk_uuid: UUID, **kwargs: Any) -> SemanticChunk:
        """Compatibility spelling for :meth:`get_by_uuid`."""
        return await self.get_by_uuid(chunk_uuid, **kwargs)

    async def _lock_root(self, chunk_uuid: UUID) -> SemanticChunkRow | None:
        return (
            await self._session.execute(
                select(SemanticChunkRow)
                .where(SemanticChunkRow.id == chunk_uuid)
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _replace_metrics(
        self,
        chunk_uuid: UUID,
        metrics: MetricsRow | None,
        feedback: FeedbackRow | None,
    ) -> None:
        if metrics is None:
            await self._session.execute(
                delete(SemanticChunkFeedback).where(
                    SemanticChunkFeedback.chunk_uuid == chunk_uuid
                )
            )
            await self._session.execute(
                delete(SemanticChunkMetrics).where(
                    SemanticChunkMetrics.chunk_uuid == chunk_uuid
                )
            )
            return

        metrics_row = (
            await self._session.execute(
                select(SemanticChunkMetrics)
                .where(SemanticChunkMetrics.chunk_uuid == chunk_uuid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        values = {key: value for key, value in metrics.items() if key != "chunk_uuid"}
        if metrics_row is None:
            self._session.add(SemanticChunkMetrics(chunk_uuid=chunk_uuid, **values))
        else:
            for key, value in values.items():
                setattr(metrics_row, key, value)
        await self._session.flush()

        feedback_row = (
            await self._session.execute(
                select(SemanticChunkFeedback)
                .where(SemanticChunkFeedback.chunk_uuid == chunk_uuid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if feedback is None:
            if feedback_row is not None:
                await self._session.delete(feedback_row)
        else:
            values = {key: value for key, value in feedback.items() if key != "chunk_uuid"}
            if feedback_row is None:
                self._session.add(SemanticChunkFeedback(chunk_uuid=chunk_uuid, **values))
            else:
                for key, value in values.items():
                    setattr(feedback_row, key, value)

    async def _replace_ordered_children(self, rows: SemanticChunkRows) -> None:
        chunk_uuid = rows.root["id"]
        await self._session.execute(
            delete(SemanticChunkToken).where(SemanticChunkToken.chunk_uuid == chunk_uuid)
        )
        await self._session.execute(
            delete(SemanticChunkTag).where(SemanticChunkTag.chunk_uuid == chunk_uuid)
        )
        await self._session.execute(
            delete(SemanticChunkLink).where(SemanticChunkLink.source_chunk_uuid == chunk_uuid)
        )
        self._session.add_all(
            [SemanticChunkToken(**dict(row)) for row in rows.tokens]
            + [SemanticChunkTag(**dict(row)) for row in rows.tags]
            + [SemanticChunkLink(**dict(row)) for row in rows.links]
        )
        await self._session.flush()

    async def _upsert_embeddings(self, embeddings: Sequence[EmbeddingRow]) -> None:
        for payload in embeddings:
            chunk_uuid = payload["chunk_uuid"]
            model = payload["model"]
            dimension = payload["dimension"]
            if payload.get("active") is True:
                await self._session.execute(
                    update(SemanticChunkEmbedding)
                    .where(
                        SemanticChunkEmbedding.chunk_uuid == chunk_uuid,
                        SemanticChunkEmbedding.model == model,
                        SemanticChunkEmbedding.dimension == dimension,
                    )
                    .values(active=False)
                )
            existing = (
                await self._session.execute(
                    select(SemanticChunkEmbedding)
                    .where(
                        SemanticChunkEmbedding.chunk_uuid == chunk_uuid,
                        SemanticChunkEmbedding.model == model,
                        SemanticChunkEmbedding.provider == payload["provider"],
                        SemanticChunkEmbedding.model_version == payload["model_version"],
                        SemanticChunkEmbedding.dimension == dimension,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            values = {
                key: value
                for key, value in payload.items()
                if key not in {"id", "chunk_uuid"}
            }
            if existing is None:
                self._session.add(SemanticChunkEmbedding(**dict(payload)))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)

    async def _read_rows(self, root: SemanticChunkRow) -> SemanticChunkRows:
        chunk_uuid = root.id
        metrics = (
            await self._session.execute(
                select(SemanticChunkMetrics).where(
                    SemanticChunkMetrics.chunk_uuid == chunk_uuid
                )
            )
        ).scalar_one_or_none()
        feedback = (
            await self._session.execute(
                select(SemanticChunkFeedback).where(
                    SemanticChunkFeedback.chunk_uuid == chunk_uuid
                )
            )
        ).scalar_one_or_none()
        token_rows = (
            await self._session.execute(
                select(SemanticChunkToken)
                .where(SemanticChunkToken.chunk_uuid == chunk_uuid)
                .order_by(SemanticChunkToken.token_kind, SemanticChunkToken.ordinal)
            )
        ).scalars().all()
        tag_rows = (
            await self._session.execute(
                select(SemanticChunkTag)
                .where(SemanticChunkTag.chunk_uuid == chunk_uuid)
                .order_by(SemanticChunkTag.ordinal)
            )
        ).scalars().all()
        link_rows = (
            await self._session.execute(
                select(SemanticChunkLink)
                .where(SemanticChunkLink.source_chunk_uuid == chunk_uuid)
                .order_by(
                    SemanticChunkLink.relation_type,
                    SemanticChunkLink.ordinal,
                    SemanticChunkLink.target_chunk_uuid,
                )
            )
        ).scalars().all()
        embedding_rows = (
            await self._session.execute(
                select(SemanticChunkEmbedding)
                .where(SemanticChunkEmbedding.chunk_uuid == chunk_uuid)
                .order_by(SemanticChunkEmbedding.created_at, SemanticChunkEmbedding.id)
            )
        ).scalars().all()
        root_payload: RootRow = {key: getattr(root, key) for key in (
            "id", "document_id", "paragraph_id", "chapter_id", "order_index", "text",
            "source_start", "source_end", "char_count", "chunk_type", "score", "search_weight",
            "block_meta",
        )}
        metrics_payload: MetricsRow | None = None
        if metrics is not None:
            metrics_payload = {key: getattr(metrics, key) for key in (
                "chunk_uuid", "quality_score", "coverage", "cohesion", "boundary_prev",
                "boundary_next", "matches", "used_in_generation", "used_as_input", "used_as_context",
            )}
        feedback_payload: FeedbackRow | None = None
        if feedback is not None:
            feedback_payload = {key: getattr(feedback, key) for key in (
                "chunk_uuid", "accepted", "rejected", "modifications",
            )}
        block_meta = split_block_meta(root.block_meta)
        return SemanticChunkRows(
            root=root_payload,
            metrics=metrics_payload,
            feedback=feedback_payload,
            tokens=tuple(TokenRow(
                chunk_uuid=row.chunk_uuid, token_kind=row.token_kind,
                ordinal=row.ordinal, token_value=row.token_value,
            ) for row in token_rows),
            tags=tuple(TagRow(
                chunk_uuid=row.chunk_uuid, ordinal=row.ordinal, tag_value=row.tag_value
            ) for row in tag_rows),
            links=tuple(LinkRow(
                source_chunk_uuid=row.source_chunk_uuid, relation_type=row.relation_type,
                target_chunk_uuid=row.target_chunk_uuid, ordinal=row.ordinal,
                relation_data=row.relation_data,
            ) for row in link_rows),
            embeddings=tuple(EmbeddingRow(
                id=row.id, chunk_uuid=row.chunk_uuid, vector=list(row.vector), model=row.model,
                dimension=row.dimension, provider=row.provider, model_version=row.model_version,
                created_at=row.created_at, active=row.active,
            ) for row in embedding_rows),
            block_meta={
                "chunk_uuid": chunk_uuid,
                "promoted": dict(block_meta.promoted),
                "extensions": dict(block_meta.extensions),
            },
        )


__all__ = ["SemanticChunkNotFoundError", "SemanticChunkRepository"]
