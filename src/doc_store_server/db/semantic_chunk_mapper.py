"""Pure mapping between adapter ``SemanticChunk`` values and row payloads.

The payloads in this module deliberately contain database column names only.
Compatibility fields are derived when an aggregate is rebuilt; they are never
stored a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence, TypedDict
from uuid import UUID

from chunk_metadata_adapter import SemanticChunk

from ..domain.semantic_chunk import deserialize_semantic_chunk, serialize_semantic_chunk
from .link_embedding_metadata_schema import (
    merge_block_meta,
    select_active_embedding,
    split_block_meta,
)
from .token_tag_schema import TOKEN_KINDS


class RootRow(TypedDict, total=False):
    id: UUID
    document_id: UUID
    paragraph_id: UUID
    chapter_id: UUID
    order_index: int
    text: str
    source_start: int
    source_end: int
    char_count: int
    chunk_type: str | None
    score: float | None
    search_weight: int
    block_meta: dict[str, Any]


class MetricsRow(TypedDict, total=False):
    chunk_uuid: UUID
    quality_score: float | None
    coverage: float | None
    cohesion: float | None
    boundary_prev: float | None
    boundary_next: float | None
    matches: int | None
    used_in_generation: bool | None
    used_as_input: bool | None
    used_as_context: bool | None


class FeedbackRow(TypedDict, total=False):
    chunk_uuid: UUID
    accepted: int | None
    rejected: int | None
    modifications: int | None


class TokenRow(TypedDict):
    chunk_uuid: UUID
    token_kind: str
    ordinal: int
    token_value: str


class TagRow(TypedDict):
    chunk_uuid: UUID
    ordinal: int
    tag_value: str


class LinkRow(TypedDict, total=False):
    source_chunk_uuid: UUID
    relation_type: str
    target_chunk_uuid: UUID
    ordinal: int
    relation_data: dict[str, Any]


class EmbeddingRow(TypedDict, total=False):
    id: UUID
    chunk_uuid: UUID
    vector: list[float]
    model: str
    dimension: int
    provider: str
    model_version: str
    created_at: datetime | None
    active: bool


class BlockMetaRow(TypedDict):
    chunk_uuid: UUID
    promoted: dict[str, Any]
    extensions: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SemanticChunkRows:
    """Immutable row bundle for one complete semantic-chunk aggregate."""

    root: RootRow
    metrics: MetricsRow | None
    feedback: FeedbackRow | None
    tokens: tuple[TokenRow, ...]
    tags: tuple[TagRow, ...]
    links: tuple[LinkRow, ...]
    embeddings: tuple[EmbeddingRow, ...]
    block_meta: BlockMetaRow


def _uuid(value: Any, field: str) -> UUID:
    try:
        result = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc
    return result


def _ordered(rows: Sequence[Mapping[str, Any]], field: str) -> tuple[Mapping[str, Any], ...]:
    values = []
    seen: set[int] = set()
    for row in rows:
        try:
            ordinal = int(row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a non-negative integer") from exc
        if ordinal < 0 or ordinal in seen:
            raise ValueError(f"invalid or duplicate {field}: {ordinal}")
        seen.add(ordinal)
        values.append((ordinal, row))
    values.sort(key=lambda item: item[0])
    if [ordinal for ordinal, _ in values] != list(range(len(values))):
        raise ValueError(f"{field} values must be contiguous from zero")
    return tuple(row for _, row in values)


def _copy_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


_METRIC_ALIAS_FIELDS = (
    "quality_score",
    "coverage",
    "cohesion",
    "boundary_prev",
    "boundary_next",
    "used_in_generation",
)


def _assert_matching_alias(payload: Mapping[str, Any], key: str, canonical: Any) -> None:
    if payload.get(key) is not None and canonical is not None and payload[key] != canonical:
        raise ValueError(f"conflicting compatibility alias: {key}")


def to_rows(
    chunk: SemanticChunk,
    *,
    embedding_provider: str = "",
    embedding_model_version: str = "",
    embedding_created_at: datetime | None = None,
    embedding_active: bool = True,
) -> SemanticChunkRows:
    """Project one validated adapter chunk into canonical persistence rows."""

    payload = serialize_semantic_chunk(chunk)
    chunk_id = _uuid(payload["uuid"], "SemanticChunk.uuid")
    block_meta = payload.get("block_meta") or {}
    if not isinstance(block_meta, Mapping):
        raise TypeError("SemanticChunk.block_meta must be a mapping")
    parts = split_block_meta(block_meta)
    chapter_id = parts.extensions.get("chapter_id", parts.promoted.get("parent_id"))
    if chapter_id is None:
        raise ValueError("SemanticChunk.block_meta must identify chapter_id")

    root: RootRow = {
        "id": chunk_id,
        "document_id": _uuid(payload["source_id"], "SemanticChunk.source_id"),
        "paragraph_id": _uuid(payload["block_id"], "SemanticChunk.block_id"),
        "chapter_id": _uuid(chapter_id, "SemanticChunk.block_meta.chapter_id"),
        "order_index": int(payload.get("ordinal", 0)),
        "text": str(payload["body"]),
        "source_start": int(payload.get("start") or 0),
        "source_end": int(payload.get("end") or 0),
        "char_count": len(str(payload["body"])),
        "chunk_type": getattr(payload.get("type"), "value", payload.get("type")),
        "score": payload.get("score"),
        "search_weight": 1,
        "block_meta": dict(block_meta),
    }

    metrics_value = payload.get("metrics")
    metrics: MetricsRow | None = None
    feedback: FeedbackRow | None = None
    if metrics_value is not None:
        metrics_payload = metrics_value if isinstance(metrics_value, Mapping) else {}
        for key in _METRIC_ALIAS_FIELDS:
            _assert_matching_alias(payload, key, metrics_payload.get(key))
        metrics = {"chunk_uuid": chunk_id}
        for key in ("quality_score", "coverage", "cohesion", "boundary_prev", "boundary_next", "matches", "used_in_generation", "used_as_input", "used_as_context"):
            metrics[key] = metrics_payload.get(key)
        feedback_value = metrics_payload.get("feedback")
        if feedback_value is not None:
            feedback_payload = feedback_value if isinstance(feedback_value, Mapping) else {}
            for key, alias in (
                ("accepted", "feedback_accepted"),
                ("rejected", "feedback_rejected"),
                ("modifications", "feedback_modifications"),
            ):
                _assert_matching_alias(payload, alias, feedback_payload.get(key))
            feedback = {"chunk_uuid": chunk_id, **{key: feedback_payload.get(key) for key in ("accepted", "rejected", "modifications")}}

    tokens: list[TokenRow] = []
    for kind in TOKEN_KINDS:
        for ordinal, value in enumerate((metrics_value or {}).get(kind) or []):
            tokens.append({"chunk_uuid": chunk_id, "token_kind": kind, "ordinal": ordinal, "token_value": str(value)})
    tags = tuple({"chunk_uuid": chunk_id, "ordinal": i, "tag_value": str(value)} for i, value in enumerate(payload.get("tags") or []))

    links: list[LinkRow] = []
    for ordinal, value in enumerate(payload.get("links") or []):
        if not isinstance(value, str) or ":" not in value:
            raise ValueError("links must use relation:uuid format")
        relation, target = value.split(":", 1)
        links.append({"source_chunk_uuid": chunk_id, "relation_type": relation, "target_chunk_uuid": _uuid(target, "link target"), "ordinal": ordinal, "relation_data": {}})

    embeddings: list[EmbeddingRow] = []
    vector = payload.get("embedding")
    model = payload.get("embedding_model")
    if vector is not None or model is not None:
        if vector is None or not model:
            raise ValueError("embedding and embedding_model must be supplied together")
        values = [float(item) for item in vector]
        embeddings.append({"chunk_uuid": chunk_id, "vector": values, "model": str(model), "dimension": len(values), "provider": embedding_provider, "model_version": embedding_model_version, "created_at": embedding_created_at, "active": embedding_active})

    block_row: BlockMetaRow = {"chunk_uuid": chunk_id, "promoted": dict(parts.promoted), "extensions": dict(parts.extensions)}
    return SemanticChunkRows(root, metrics, feedback, tuple(tokens), tags, tuple(links), tuple(embeddings), block_row)


def from_rows(
    rows: SemanticChunkRows,
    *,
    requested_model: str | None = None,
    requested_dimension: int | None = None,
) -> SemanticChunk:
    """Rebuild an adapter chunk from canonical rows without persistence access."""

    root = rows.root
    chunk_id = _uuid(root["id"], "root.id")
    if rows.block_meta["chunk_uuid"] != chunk_id:
        raise ValueError("block_meta row has invalid child ownership")
    for collection_name, collection in (("tokens", rows.tokens), ("tags", rows.tags), ("links", rows.links), ("embeddings", rows.embeddings)):
        for row in collection:
            owner = row.get("chunk_uuid", row.get("source_chunk_uuid"))
            if owner != chunk_id:
                raise ValueError(f"{collection_name} row has invalid child ownership")

    ordered_tokens = {kind: [] for kind in TOKEN_KINDS}
    grouped: dict[str, list[Mapping[str, Any]]] = {kind: [] for kind in TOKEN_KINDS}
    for row in rows.tokens:
        kind = row["token_kind"]
        if kind not in grouped:
            raise ValueError(f"unknown token kind: {kind}")
        grouped[kind].append(row)
    for kind in TOKEN_KINDS:
        ordered_tokens[kind] = [row["token_value"] for row in _ordered(grouped[kind], "ordinal")]
    ordered_tags = [row["tag_value"] for row in _ordered(rows.tags, "ordinal")]
    ordered_link_rows = _ordered(rows.links, "ordinal")
    ordered_links = [f"{row['relation_type']}:{row['target_chunk_uuid']}" for row in ordered_link_rows]

    meta = merge_block_meta(rows.block_meta["promoted"], rows.block_meta["extensions"])
    metrics_payload: dict[str, Any] | None = None
    if rows.metrics is not None:
        metrics_payload = {key: value for key, value in rows.metrics.items() if key != "chunk_uuid"}
        if rows.feedback is not None:
            if rows.feedback["chunk_uuid"] != chunk_id:
                raise ValueError("feedback row has invalid child ownership")
            metrics_payload["feedback"] = {key: value for key, value in rows.feedback.items() if key != "chunk_uuid"}
        metrics_payload.update(ordered_tokens)

    selected = None
    if requested_model is not None or requested_dimension is not None:
        if requested_model is None or requested_dimension is None:
            raise ValueError("requested_model and requested_dimension must be supplied together")
        selected = select_active_embedding(rows.embeddings, requested_model, requested_dimension)
    elif rows.embeddings:
        active = [row for row in rows.embeddings if row.get("active") is True]
        selected = max(active, key=lambda row: (row.get("created_at") or datetime.min, str(row.get("id", "")))) if active else None

    payload: dict[str, Any] = {
        "uuid": str(chunk_id), "source_id": str(root["document_id"]), "block_id": str(root["paragraph_id"]),
        "type": root.get("chunk_type") or "DocBlock",
        "body": root["text"], "text": root["text"], "ordinal": root["order_index"], "start": root["source_start"], "end": root["source_end"],
        "block_meta": meta, "tags": ordered_tags or None, "tags_flat": ", ".join(ordered_tags) or None, "links": ordered_links or None,
    }
    if metrics_payload is not None:
        payload["metrics"] = metrics_payload
        for key in _METRIC_ALIAS_FIELDS:
            payload[key] = metrics_payload.get(key)
        feedback_payload = metrics_payload.get("feedback")
        if isinstance(feedback_payload, Mapping):
            payload["feedback_accepted"] = feedback_payload.get("accepted")
            payload["feedback_rejected"] = feedback_payload.get("rejected")
            payload["feedback_modifications"] = feedback_payload.get("modifications")
    if ordered_link_rows:
        related = [
            str(row["target_chunk_uuid"])
            for row in ordered_link_rows
            if row.get("relation_type") == "related"
        ]
        parents = [
            str(row["target_chunk_uuid"])
            for row in ordered_link_rows
            if row.get("relation_type") == "parent"
        ]
        if related:
            payload["link_related"] = related[0]
        if parents:
            payload["link_parent"] = parents[0]
    if selected is not None:
        payload["embedding"] = list(selected["vector"])
        payload["embedding_model"] = selected["model"]
    return deserialize_semantic_chunk(payload)


map_to_rows = to_rows
map_from_rows = from_rows

__all__ = ["BlockMetaRow", "EmbeddingRow", "FeedbackRow", "LinkRow", "MetricsRow", "RootRow", "SemanticChunkRows", "TagRow", "TokenRow", "from_rows", "map_from_rows", "map_to_rows", "to_rows"]
