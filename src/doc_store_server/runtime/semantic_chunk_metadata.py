"""Safe public update boundary for SemanticChunk classifier metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID, uuid4
import json
import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.previews import chunk_preview


CLASSIFIER_FIELDS: dict[str, tuple[str, str, str, str]] = {
    "type": (
        "chunk_type_id",
        "chunk_types",
        "semantic_chunk_type_assignments",
        "chunk_type",
    ),
    "role": (
        "role_id",
        "chunk_roles",
        "semantic_chunk_role_assignments",
        "role",
    ),
    "status": (
        "status_id",
        "chunk_statuses",
        "semantic_chunk_status_assignments",
        "status",
    ),
    "block_type": (
        "block_type_id",
        "block_types",
        "semantic_chunk_block_type_assignments",
        "block_type",
    ),
    "language": (
        "language_id",
        "languages",
        "semantic_chunk_language_assignments",
        "language",
    ),
    "category": (
        "category_id",
        "categories",
        "semantic_chunk_category_assignments",
        "category",
    ),
}
SAFE_METADATA_FIELDS = frozenset(
    {
        *CLASSIFIER_FIELDS,
        "tags",
        "summary",
        "title",
        "classification",
    }
)
FORBIDDEN_METADATA_FIELDS = frozenset(
    {
        "quality_score",
        "coverage",
        "cohesion",
        "boundary_prev",
        "boundary_next",
        "embedding",
        "embedding_model",
        "body",
        "text",
        "tokens",
        "bm25_tokens",
    }
)
DEFAULT_CLASSIFIER_VALUES = {
    "type": "DocBlock",
    "role": "system",
    "status": "new",
    "block_type": "paragraph",
    "language": "UNKNOWN",
    "category": "uncategorized",
}


class SemanticChunkMetadataService:
    """Update only metadata fields that do not rewrite chunk text or embeddings."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    def update_metadata(
        self,
        *,
        updates: Mapping[str, Any] | None = None,
        items: Sequence[Mapping[str, Any]] | None = None,
        chunk_id: str | None = None,
        chunk_ids: Sequence[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
        include_deleted: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if items is not None and updates is not None:
            raise ValueError("items and updates are mutually exclusive")
        if items is None and updates is None:
            raise ValueError("updates or items is required")
        with self._transaction() as connection:
            if items is not None:
                normalized_items = _validated_items(items)
                targets = [
                    (item["chunk_id"], _validated_updates(item["updates"]))
                    for item in normalized_items
                ]
            else:
                patch = _validated_updates(updates or {})
                selected = _select_chunk_ids(
                    connection,
                    chunk_id=chunk_id,
                    chunk_ids=chunk_ids,
                    filters=filters or {},
                    limit=limit,
                    offset=offset,
                    include_deleted=include_deleted,
                )
                targets = [(selected_id, patch) for selected_id in selected]
            results = []
            for selected_id, patch in targets:
                results.append(
                    self._update_one(
                        connection,
                        chunk_id=selected_id,
                        updates=patch,
                        dry_run=dry_run,
                    )
                )
        updated = sum(1 for item in results if item["outcome"] == "updated")
        return {
            "outcome": "dry_run" if dry_run else "updated",
            "requested": len(targets),
            "matched": len(results),
            "updated": 0 if dry_run else updated,
            "items": results,
            "dry_run": dry_run,
        }

    def _update_one(
        self,
        connection: Connection,
        *,
        chunk_id: str,
        updates: Mapping[str, Any],
        dry_run: bool,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                "SELECT sc.id, sct.text, sc.block_meta, sc.is_deleted "
                "FROM semantic_chunks AS sc "
                "JOIN semantic_chunk_texts AS sct ON sct.chunk_uuid = sc.id "
                "WHERE sc.id = CAST(:chunk_id AS uuid)"
            ),
            {"chunk_id": chunk_id},
        ).mappings().one_or_none()
        if row is None:
            raise LookupError(chunk_id)
        block_meta = row["block_meta"]
        if not isinstance(block_meta, Mapping):
            block_meta = {}
        merged_meta = dict(block_meta)
        classifier_ids: dict[str, UUID] = {}
        for field in CLASSIFIER_FIELDS:
            if field in updates:
                value = str(updates[field])
                classifier_ids[field] = _dictionary_id(
                    connection,
                    CLASSIFIER_FIELDS[field][1],
                    value,
                )
                merged_meta[field] = value
        if "tags" in updates:
            tags = tuple(_validated_tags(updates["tags"]))
            merged_meta["tags"] = list(tags)
            merged_meta["tags_flat"] = ", ".join(tags)
        if "summary" in updates:
            merged_meta["summary"] = updates["summary"]
        if "title" in updates:
            merged_meta["title"] = updates["title"]
        if "classification" in updates:
            merged_meta["classification"] = updates["classification"]
        result = {
            "chunk_id": str(row["id"]),
            "preview": chunk_preview(str(row["text"])),
            "outcome": "dry_run" if dry_run else "updated",
            "updated_fields": sorted(updates),
            "metadata": {key: merged_meta.get(key) for key in sorted(updates)},
        }
        if dry_run:
            return result
        assignments = ["block_meta = CAST(:block_meta AS jsonb)"]
        params: dict[str, Any] = {
            "chunk_id": str(row["id"]),
            "block_meta": json.dumps(merged_meta, ensure_ascii=False),
        }
        for field, dictionary_id in classifier_ids.items():
            column, _dictionary, _assignment, _metadata = CLASSIFIER_FIELDS[field]
            assignments.append(f"{column} = :{column}")
            params[column] = dictionary_id
            if field == "type":
                assignments.append("chunk_type = :chunk_type")
                params["chunk_type"] = str(updates[field])
        connection.execute(
            text(
                "UPDATE semantic_chunks SET "
                f"{', '.join(assignments)} "
                "WHERE id = CAST(:chunk_id AS uuid)"
            ),
            params,
        )
        for field, dictionary_id in classifier_ids.items():
            column, _dictionary, table, _metadata = CLASSIFIER_FIELDS[field]
            connection.execute(
                text(
                    f"INSERT INTO {table} (chunk_uuid, {column}) "
                    "VALUES (CAST(:chunk_id AS uuid), :dictionary_id) "
                    "ON CONFLICT (chunk_uuid) DO UPDATE SET "
                    f"{column} = EXCLUDED.{column}, "
                    "updated_at = now()"
                ),
                {"chunk_id": str(row["id"]), "dictionary_id": dictionary_id},
            )
        if "tags" in updates:
            tags = tuple(_validated_tags(updates["tags"]))
            connection.execute(
                text("DELETE FROM semantic_chunk_tags WHERE chunk_uuid = CAST(:chunk_id AS uuid)"),
                {"chunk_id": str(row["id"])},
            )
            for ordinal, tag_value in enumerate(tags):
                connection.execute(
                    text(
                        "INSERT INTO semantic_chunk_tags "
                        "(chunk_uuid, ordinal, tag_value) "
                        "VALUES (CAST(:chunk_id AS uuid), :ordinal, :tag_value)"
                    ),
                    {
                        "chunk_id": str(row["id"]),
                        "ordinal": ordinal,
                        "tag_value": tag_value,
                    },
                )
        return result

    def _engine(self) -> Any:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        return create_engine(self._database_url, pool_pre_ping=True)

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        engine = self._engine()
        try:
            with engine.begin() as connection:
                yield connection
        finally:
            engine.dispose()


def installed_semantic_chunk_metadata_service(
    config: Mapping[str, Any] | None = None,
) -> SemanticChunkMetadataService | None:
    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return SemanticChunkMetadataService(database_url)


def _validated_updates(updates: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(updates, Mapping):
        raise ValueError("updates must be an object")
    unknown = sorted(set(updates) - SAFE_METADATA_FIELDS - FORBIDDEN_METADATA_FIELDS)
    if unknown:
        raise ValueError(f"unsupported SemanticChunk metadata fields: {', '.join(unknown)}")
    forbidden = sorted(set(updates) & FORBIDDEN_METADATA_FIELDS)
    if forbidden:
        raise ValueError(f"forbidden SemanticChunk metadata fields: {', '.join(forbidden)}")
    payload: dict[str, Any] = {}
    for field, value in updates.items():
        if field in CLASSIFIER_FIELDS:
            payload[field] = _validated_descr(
                value,
                default=DEFAULT_CLASSIFIER_VALUES[field],
                field=field,
            )
        elif field == "tags":
            payload[field] = tuple(_validated_tags(value))
        elif field in {"summary", "title"}:
            payload[field] = _validated_optional_text(value, field)
        elif field == "classification":
            payload[field] = _validated_classification(value)
    if not payload:
        raise ValueError("updates must not be empty")
    return payload


def _validated_items(items: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise ValueError("items must be an array")
    if not items:
        raise ValueError("items must not be empty")
    if len(items) > 1000:
        raise ValueError("items must contain at most 1000 entries")
    result = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"items[{index}] must be an object")
        try:
            chunk_id = str(UUID(str(item["chunk_id"])))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"items[{index}].chunk_id must be a UUID") from exc
        updates = item.get("updates")
        if not isinstance(updates, Mapping):
            raise ValueError(f"items[{index}].updates must be an object")
        result.append({"chunk_id": chunk_id, "updates": updates})
    return tuple(result)


def _select_chunk_ids(
    connection: Connection,
    *,
    chunk_id: str | None,
    chunk_ids: Sequence[str] | None,
    filters: Mapping[str, Any],
    limit: int,
    offset: int,
    include_deleted: bool,
) -> tuple[str, ...]:
    selectors = sum(
        1
        for value in (chunk_id, chunk_ids, filters)
        if value not in (None, {}, ())
    )
    if selectors != 1:
        raise ValueError("select exactly one of chunk_id, chunk_ids, or filters")
    if chunk_id is not None:
        return (str(UUID(str(chunk_id))),)
    if chunk_ids is not None:
        if isinstance(chunk_ids, (str, bytes)) or not isinstance(chunk_ids, Sequence):
            raise ValueError("chunk_ids must be an array")
        if not chunk_ids:
            raise ValueError("chunk_ids must not be empty")
        if len(chunk_ids) > 1000:
            raise ValueError("chunk_ids must contain at most 1000 entries")
        return tuple(str(UUID(str(item))) for item in chunk_ids)
    where: list[str] = []
    params: dict[str, Any] = {}
    if not include_deleted:
        where.append("is_deleted IS FALSE")
    for key, value in filters.items():
        if key in {"document_id", "paragraph_id", "chapter_id"}:
            where.append(f"{key} = CAST(:{key} AS uuid)")
            params[key] = str(UUID(str(value)))
        elif key in {"file_id", "project_id", "source_name"}:
            where.append(f"block_meta ->> :{key}_key = :{key}")
            params[f"{key}_key"] = key
            params[key] = str(value)
        elif key in {"seven_d_number", "7d_number"}:
            where.append("(block_meta ->> '7d_number') ~ '^[0-9]+$'")
            where.append("(block_meta ->> '7d_number')::int = :seven_d_number")
            params["seven_d_number"] = int(value)
        elif key == "seven_d_min":
            where.append("(block_meta ->> '7d_number') ~ '^[0-9]+$'")
            where.append("(block_meta ->> '7d_number')::int >= :seven_d_min")
            params["seven_d_min"] = int(value)
        elif key == "seven_d_max":
            where.append("(block_meta ->> '7d_number') ~ '^[0-9]+$'")
            where.append("(block_meta ->> '7d_number')::int <= :seven_d_max")
            params["seven_d_max"] = int(value)
        else:
            raise ValueError(f"unsupported filter: {key}")
    if not where:
        raise ValueError("filters must not be empty")
    params["limit"] = _limit(limit)
    params["offset"] = _offset(offset)
    rows = connection.execute(
        text(
            "SELECT id FROM semantic_chunks "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY document_id ASC, order_index ASC, id ASC "
            "LIMIT :limit OFFSET :offset"
        ),
        params,
    ).scalars().all()
    return tuple(str(row) for row in rows)


def _dictionary_id(connection: Connection, table: str, descr: str) -> UUID:
    if table not in {
        "chunk_types",
        "chunk_roles",
        "chunk_statuses",
        "block_types",
        "languages",
        "categories",
    }:
        raise ValueError(f"unsupported dictionary table: {table}")
    row = connection.execute(
        text(f"SELECT id FROM {table} WHERE descr = :descr"),
        {"descr": descr},
    ).scalar_one_or_none()
    if row is not None:
        return row
    return connection.execute(
        text(
            f"INSERT INTO {table} (id, descr, is_deleted, deleted_at) "
            "VALUES (:id, :descr, FALSE, NULL) "
            "ON CONFLICT (descr) DO UPDATE SET "
            "is_deleted = FALSE, deleted_at = NULL, updated_at = now() "
            "RETURNING id"
        ),
        {"id": uuid4(), "descr": descr},
    ).scalar_one()


def _validated_descr(value: Any, *, default: str, field: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip() or default
    if len(result) > 100:
        raise ValueError(f"{field} must be at most 100 characters")
    return result


def _validated_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",")]
    elif isinstance(value, Sequence):
        raw = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("tags must contain strings")
            raw.append(item.strip())
    else:
        raise ValueError("tags must be an array of strings")
    result = tuple(dict.fromkeys(item for item in raw if item))
    if len(result) > 256:
        raise ValueError("tags must contain at most 256 entries")
    if any(len(item) > 256 for item in result):
        raise ValueError("tags entries must be at most 256 characters")
    return result


def _validated_optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    result = value.strip()
    if len(result) > 2048:
        raise ValueError(f"{field} must be at most 2048 characters")
    return result or None


def _validated_classification(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("classification must be an object")
    allowed = {
        "provider",
        "model",
        "model_version",
        "prompt_version",
        "confidence",
        "evidence",
        "review_status",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unsupported classification fields: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for key in ("provider", "model", "model_version", "prompt_version", "evidence", "review_status"):
        if key not in value or value[key] is None:
            continue
        if not isinstance(value[key], str):
            raise ValueError(f"classification.{key} must be a string")
        text_value = value[key].strip()
        if text_value:
            result[key] = text_value[:2048 if key == "evidence" else 256]
    if "confidence" in value and value["confidence"] is not None:
        confidence = float(value["confidence"])
        if confidence < 0 or confidence > 1:
            raise ValueError("classification.confidence must be between 0 and 1")
        result["confidence"] = confidence
    if not result:
        raise ValueError("classification must not be empty")
    result.setdefault("review_status", "machine")
    return result


def _limit(value: int) -> int:
    if isinstance(value, bool) or value < 1 or value > 10000:
        raise ValueError("limit must be between 1 and 10000")
    return int(value)


def _offset(value: int) -> int:
    if isinstance(value, bool) or value < 0 or value > 10_000_000:
        raise ValueError("offset must be between 0 and 10000000")
    return int(value)


__all__ = [
    "FORBIDDEN_METADATA_FIELDS",
    "SAFE_METADATA_FIELDS",
    "SemanticChunkMetadataService",
    "installed_semantic_chunk_metadata_service",
]
