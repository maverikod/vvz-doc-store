"""Corpus-wide semantic relation discovery over stored embeddings."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import os
import re
from statistics import fmean
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.previews import chunk_preview


_IDENTIFIER_RE = re.compile(r"\b(7d-(?P<number>\d+)(?:-[\w.-]+)?)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RelationItem:
    """One item participating in a semantic relation group."""

    id: str
    level: str
    document_id: str
    chunk_id: str
    paragraph_id: str | None
    source_name: str | None
    seven_d_number: int | None
    paragraph_number: int | None
    order_index: int | None
    preview: str


class SemanticRelationService:
    """Find similar or opposite indexed units from active pgvector embeddings."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    def search(
        self,
        *,
        level: str = "chunk",
        relation: str = "similar",
        metric: str = "cosine_distance",
        threshold: float | None = None,
        project: str | None = None,
        document_id: str | None = None,
        source_name: str | None = None,
        seven_d_number: int | None = None,
        include_deleted: bool = False,
        max_candidates: int = 300,
        max_pairs: int = 1000,
        min_group_size: int = 2,
        max_group_size: int = 20,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        level = _validated_choice(level, {"document", "file", "paragraph", "chunk"}, "level")
        relation = _validated_choice(relation, {"similar", "opposite"}, "relation")
        metric = _validated_choice(metric, {"cosine_distance", "cosine_similarity"}, "metric")
        threshold_value = _threshold(metric=metric, relation=relation, threshold=threshold)
        max_candidates = _bounded_int(max_candidates, "max_candidates", 2, 2000)
        max_pairs = _bounded_int(max_pairs, "max_pairs", 1, 10000)
        min_group_size = _bounded_int(min_group_size, "min_group_size", 2, 100)
        max_group_size = _bounded_int(max_group_size, "max_group_size", min_group_size, 100)
        limit = _bounded_int(limit, "limit", 1, 200)
        offset = _bounded_int(offset, "offset", 0, 100000)

        params: dict[str, Any] = {
            "max_candidates": max_candidates,
            "max_pairs": max_pairs,
        }
        where = ["sce.active IS TRUE"]
        if not include_deleted:
            where.extend(["sc.deleted_at IS NULL", "d.deleted_at IS NULL"])
        if project is not None:
            params["project"] = project
            where.append("(sc.block_meta ->> 'project' = :project OR sc.block_meta ->> 'project_id' = :project)")
        if document_id is not None:
            params["document_id"] = document_id
            where.append("sc.document_id = CAST(:document_id AS uuid)")
        if source_name is not None:
            params["source_name"] = source_name
            where.append("(d.source_name = :source_name OR d.source_path = :source_name)")
        if seven_d_number is not None:
            params["seven_d_number"] = f"7d-{seven_d_number:02d}"
            where.append("(d.source_name ILIKE :seven_d_number || '%' OR sct.text ILIKE :seven_d_number || '%')")

        item_expr = _item_id_expr(level)
        sql = f"""
            WITH candidates AS (
                SELECT sc.id::text AS chunk_id,
                       sc.document_id::text AS document_id,
                       sc.paragraph_id::text AS paragraph_id,
                       sc.order_index,
                       sct.text,
                       sc.block_meta,
                       d.source_name,
                       d.source_path,
                       sce.provider,
                       sce.model,
                       sce.model_version,
                       sce.dimension,
                       sce.vector,
                       {item_expr} AS item_id
                FROM semantic_chunks AS sc
                JOIN semantic_chunk_texts AS sct ON sct.chunk_uuid = sc.id
                JOIN semantic_chunk_embeddings AS sce ON sce.chunk_uuid = sc.id
                JOIN documents AS d ON d.id = sc.document_id
                WHERE {' AND '.join(where)}
                ORDER BY d.created_at ASC, sc.order_index ASC, sc.id ASC
                LIMIT :max_candidates
            )
            SELECT c1.item_id AS left_item_id,
                   c2.item_id AS right_item_id,
                   c1.chunk_id AS left_chunk_id,
                   c2.chunk_id AS right_chunk_id,
                   c1.document_id AS left_document_id,
                   c2.document_id AS right_document_id,
                   c1.paragraph_id AS left_paragraph_id,
                   c2.paragraph_id AS right_paragraph_id,
                   c1.order_index AS left_order_index,
                   c2.order_index AS right_order_index,
                   c1.text AS left_text,
                   c2.text AS right_text,
                   c1.block_meta AS left_block_meta,
                   c2.block_meta AS right_block_meta,
                   COALESCE(c1.source_name, c1.source_path) AS left_source_name,
                   COALESCE(c2.source_name, c2.source_path) AS right_source_name,
                   c1.provider,
                   c1.model,
                   c1.model_version,
                   c1.dimension,
                   (c1.vector <=> c2.vector) AS distance
            FROM candidates AS c1
            JOIN candidates AS c2
             ON c1.item_id < c2.item_id
             AND c1.provider = c2.provider
             AND c1.model = c2.model
             AND c1.model_version = c2.model_version
             AND c1.dimension = c2.dimension
            ORDER BY {"distance ASC" if relation == "similar" else "distance DESC"}
            LIMIT :max_pairs
        """
        with self._connect() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
        filtered = [
            row for row in rows
            if _matches_threshold(
                float(row["distance"]),
                metric=metric,
                relation=relation,
                threshold=threshold_value,
            )
        ]
        groups = _groups_from_rows(
            filtered,
            level=level,
            relation=relation,
            metric=metric,
            threshold=threshold_value,
            min_group_size=min_group_size,
            max_group_size=max_group_size,
        )
        return {
            "status": "ok",
            "scope": {
                "level": level,
                "project": project,
                "document_id": document_id,
                "source_name": source_name,
                "7d_number": seven_d_number,
                "include_deleted": include_deleted,
            },
            "metric": metric,
            "threshold": threshold_value,
            "relation": relation,
            "model": _first_value(filtered, "model"),
            "provider": _first_value(filtered, "provider"),
            "model_version": _first_value(filtered, "model_version"),
            "dimension": _first_value(filtered, "dimension"),
            "groups": groups[offset: offset + limit],
            "pagination": {"limit": limit, "offset": offset, "total": len(groups)},
            "diagnostics": {
                "candidate_count_limit": max_candidates,
                "pair_count_limit": max_pairs,
                "matched_pair_count": len(filtered),
                "unit_title_editing": unit_title_edit_capabilities(),
            },
        }

    def _engine(self) -> Any:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        return create_engine(self._database_url, pool_pre_ping=True)

    @contextmanager
    def _connect(self) -> Iterator[Connection]:
        engine = self._engine()
        try:
            with engine.connect() as connection:
                yield connection
        finally:
            engine.dispose()


def installed_semantic_relation_service(config: Mapping[str, Any] | None = None) -> SemanticRelationService | None:
    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return SemanticRelationService(database_url)


def unit_title_edit_capabilities() -> dict[str, Any]:
    """Return the current public ability to edit unit titles."""

    return {
        "documents": {
            "direct_field": "title",
            "editable_via": "entity_update",
            "supported": True,
        },
        "chapters": {
            "direct_field": "heading",
            "editable_via": None,
            "supported": False,
            "reason": "chapter rows are readable/listable but not root CRUD update targets",
        },
        "paragraphs": {
            "direct_field": None,
            "metadata_field": "block_meta.title",
            "editable_via": None,
            "supported": False,
            "reason": "paragraph text/title changes require an ingestion/rechunking contract",
        },
        "semantic_chunks": {
            "direct_field": None,
            "metadata_field": "block_meta.title",
            "editable_via": None,
            "supported": False,
            "reason": "chunk metadata changes require a chunk update contract to preserve indexes and embeddings",
        },
    }


def _item_id_expr(level: str) -> str:
    if level == "document":
        return "sc.document_id::text"
    if level == "paragraph":
        return "sc.paragraph_id::text"
    if level == "file":
        return "COALESCE(sc.block_meta ->> 'file_id', d.source_name, d.source_path, sc.document_id::text)"
    return "sc.id::text"


def _groups_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    level: str,
    relation: str,
    metric: str,
    threshold: float,
    min_group_size: int,
    max_group_size: int,
) -> list[dict[str, Any]]:
    parent: dict[str, str] = {}
    items: dict[str, RelationItem] = {}
    scores: dict[tuple[str, str], tuple[float, float]] = {}
    for row in rows:
        left = str(row["left_item_id"])
        right = str(row["right_item_id"])
        _union(parent, left, right)
        items.setdefault(left, _item_from_row(row, side="left", level=level))
        items.setdefault(right, _item_from_row(row, side="right", level=level))
        distance = float(row["distance"])
        scores[tuple(sorted((left, right)))] = (distance, 1.0 - distance)
    grouped: dict[str, list[str]] = {}
    for item_id in items:
        grouped.setdefault(_find(parent, item_id), []).append(item_id)
    result: list[dict[str, Any]] = []
    for index, member_ids in enumerate(sorted(grouped.values(), key=lambda values: (values[0], len(values))), 1):
        if len(member_ids) < min_group_size:
            continue
        trimmed = member_ids[:max_group_size]
        group_scores = [
            value for pair, value in scores.items()
            if pair[0] in trimmed and pair[1] in trimmed
        ]
        score_values = [similarity if metric == "cosine_similarity" else distance for distance, similarity in group_scores]
        representative = trimmed[0]
        result.append(
            {
                "group_id": f"{relation}-{index:04d}",
                "representative_id": representative,
                "item_count": len(trimmed),
                "reason": f"{relation} by {metric} threshold {threshold}",
                "min_score": min(score_values) if score_values else None,
                "max_score": max(score_values) if score_values else None,
                "avg_score": fmean(score_values) if score_values else None,
                "items": [
                    {
                        **asdict(items[item_id]),
                        "similarity": _best_item_score(item_id, trimmed, scores, similarity=True),
                        "distance": _best_item_score(item_id, trimmed, scores, similarity=False),
                    }
                    for item_id in trimmed
                ],
            }
        )
    return result


def _item_from_row(row: Mapping[str, Any], *, side: str, level: str) -> RelationItem:
    text_value = str(row[f"{side}_text"] or "")
    source_name = row.get(f"{side}_source_name")
    return RelationItem(
        id=str(row[f"{side}_item_id"]),
        level=level,
        document_id=str(row[f"{side}_document_id"]),
        chunk_id=str(row[f"{side}_chunk_id"]),
        paragraph_id=str(row.get(f"{side}_paragraph_id") or "") or None,
        source_name=str(source_name) if source_name is not None else None,
        seven_d_number=_parse_7d_number(source_name, text_value),
        paragraph_number=_int_or_none(row.get(f"{side}_order_index")),
        order_index=_int_or_none(row.get(f"{side}_order_index")),
        preview=_preview(text_value),
    )


def _best_item_score(
    item_id: str,
    group_ids: Iterable[str],
    scores: Mapping[tuple[str, str], tuple[float, float]],
    *,
    similarity: bool,
) -> float | None:
    values = [
        pair_scores[1 if similarity else 0]
        for other in group_ids
        if other != item_id
        for pair, pair_scores in ((tuple(sorted((item_id, other))), scores.get(tuple(sorted((item_id, other))))),)
        if pair_scores is not None
    ]
    if not values:
        return None
    return max(values) if similarity else min(values)


def _find(parent: dict[str, str], value: str) -> str:
    parent.setdefault(value, value)
    if parent[value] != value:
        parent[value] = _find(parent, parent[value])
    return parent[value]


def _union(parent: dict[str, str], left: str, right: str) -> None:
    left_root = _find(parent, left)
    right_root = _find(parent, right)
    if left_root != right_root:
        parent[right_root] = left_root


def _matches_threshold(distance: float, *, metric: str, relation: str, threshold: float) -> bool:
    if metric == "cosine_similarity":
        similarity = 1.0 - distance
        return similarity >= threshold if relation == "similar" else similarity <= threshold
    return distance <= threshold if relation == "similar" else distance >= threshold


def _threshold(*, metric: str, relation: str, threshold: float | None) -> float:
    if threshold is None:
        if metric == "cosine_similarity":
            return 0.82 if relation == "similar" else 0.18
        return 0.18 if relation == "similar" else 0.82
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise ValueError("threshold must be a number")
    value = float(threshold)
    if value < 0.0 or value > 2.0:
        raise ValueError("threshold must be between 0 and 2")
    return value


def _bounded_int(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validated_choice(value: str, allowed: set[str], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return value


def _parse_7d_number(*values: Any) -> int | None:
    for value in values:
        if not isinstance(value, str):
            continue
        match = _IDENTIFIER_RE.search(value)
        if match:
            return int(match.group("number"))
    return None


def _preview(value: str, *, limit: int = 220) -> str:
    return chunk_preview(value, limit=limit)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _first_value(rows: Sequence[Mapping[str, Any]], key: str) -> Any:
    return rows[0].get(key) if rows else None


__all__ = [
    "SemanticRelationService",
    "installed_semantic_relation_service",
    "unit_title_edit_capabilities",
]
