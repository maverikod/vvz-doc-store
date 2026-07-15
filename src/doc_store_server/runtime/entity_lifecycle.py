"""Runtime CRUD and lifecycle service for addressable entity rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import os
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from doc_store_server.db.health import database_url_from_config


ENTITY_TABLES: dict[str, str] = {
    "document": "documents",
    "documents": "documents",
    "chapter": "chapters",
    "chapters": "chapters",
    "paragraph": "paragraphs",
    "paragraphs": "paragraphs",
    "chunk": "semantic_chunks",
    "chunks": "semantic_chunks",
    "semantic_chunk": "semantic_chunks",
    "semantic_chunks": "semantic_chunks",
    "project": "projects",
    "projects": "projects",
}

DEFAULT_FIELDS: dict[str, tuple[str, ...]] = {
    "documents": ("id", "title", "source_name", "processing_status", "is_deleted", "updated_at", "block_meta"),
    "chapters": ("id", "document_id", "order_index", "heading", "is_deleted", "block_meta"),
    "paragraphs": ("id", "document_id", "chapter_id", "order_index", "text", "is_deleted", "block_meta"),
    "semantic_chunks": ("id", "document_id", "paragraph_id", "chapter_id", "order_index", "text", "is_deleted", "block_meta"),
}

LIST_TABLES = frozenset(DEFAULT_FIELDS)

ORDER_BY: dict[str, str] = {
    "documents": "updated_at DESC NULLS LAST, id ASC",
    "chapters": "order_index ASC NULLS LAST, id ASC",
    "paragraphs": "order_index ASC NULLS LAST, id ASC",
    "semantic_chunks": "order_index ASC NULLS LAST, id ASC",
}


class DeletionSafetyError(RuntimeError):
    """Raised when hard delete would leave references outside the delete set."""


@dataclass(frozen=True, slots=True)
class EntityRef:
    table: str
    id: UUID

    def as_key(self) -> tuple[str, UUID]:
        return self.table, self.id


class EntityLifecycleService:
    """DB-backed implementation of common CRUD/lifecycle operations."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    def list_entities(
        self,
        *,
        entity_type: str,
        fields: Sequence[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        limit: int = 50,
        offset: int = 0,
        show_deleted: bool = False,
    ) -> dict[str, Any]:
        table = _entity_table(entity_type)
        if table == "projects":
            return self._list_projects(limit=limit, offset=offset, show_deleted=show_deleted)
        if table not in LIST_TABLES:
            raise ValueError(f"unsupported list entity_type: {entity_type}")
        selected = _validated_fields(table, fields)
        where, params = _filters_sql(table, filters or {})
        if not show_deleted:
            where.append("is_deleted IS FALSE")
        sql = (
            f"SELECT {', '.join(selected)} FROM {table} "
            f"{'WHERE ' + ' AND '.join(where) if where else ''} "
            f"ORDER BY {ORDER_BY[table]} LIMIT :limit OFFSET :offset"
        )
        count_sql = f"SELECT count(*) FROM {table} {'WHERE ' + ' AND '.join(where) if where else ''}"
        params.update({"limit": _limit(limit), "offset": _offset(offset)})
        with self._connect() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
            total = int(connection.execute(text(count_sql), params).scalar_one())
        return {
            "entity_type": table,
            "items": [_json_row(row) for row in rows],
            "limit": params["limit"],
            "offset": params["offset"],
            "total": total,
            "show_deleted": show_deleted,
        }

    def get_entity(
        self,
        *,
        entity_type: str,
        entity_id: str,
        fields: Sequence[str] | None = None,
        show_deleted: bool = False,
    ) -> dict[str, Any]:
        table = _entity_table(entity_type)
        if table == "projects":
            raise LookupError(entity_id)
        selected = _validated_fields(table, fields)
        where = ["id = CAST(:entity_id AS uuid)"]
        if not show_deleted:
            where.append("is_deleted IS FALSE")
        with self._connect() as connection:
            row = connection.execute(
                text(f"SELECT {', '.join(selected)} FROM {table} WHERE {' AND '.join(where)}"),
                {"entity_id": str(UUID(entity_id))},
            ).mappings().one_or_none()
        if row is None:
            raise LookupError(entity_id)
        return {"entity_type": table, "id": entity_id, "value": _json_row(row)}

    def soft_delete(self, *, entity_type: str, ids: Sequence[str]) -> dict[str, Any]:
        return self._mark_deleted(entity_type=entity_type, ids=ids, deleted=True)

    def undelete(self, *, entity_type: str, ids: Sequence[str]) -> dict[str, Any]:
        return self._mark_deleted(entity_type=entity_type, ids=ids, deleted=False)

    def hard_delete(self, *, entity_type: str, ids: Sequence[str]) -> dict[str, Any]:
        table = _entity_table(entity_type)
        roots = self._roots_for(table, ids)
        if not roots:
            return {"outcome": "deleted", "deleted": {}, "blocked": []}
        with self._transaction() as connection:
            closure = self._closure(connection, roots)
            blockers = self.references_to(connection, closure)
            if blockers:
                raise DeletionSafetyError("unsafe hard delete: external references exist")
            deleted = self._delete_closure(connection, closure)
        return {"outcome": "deleted", "deleted": deleted, "blocked": []}

    def references_for(self, *, entity_type: str, entity_id: str) -> dict[str, Any]:
        table = _entity_table(entity_type)
        if table == "projects":
            roots = self._roots_for(table, [entity_id])
            refs = {root.as_key() for root in roots}
        else:
            refs = {EntityRef(table, UUID(entity_id)).as_key()}
        with self._connect() as connection:
            references = self.references_to(connection, refs)
        return {"entity_type": table, "id": entity_id, "references": references}

    def references_to(
        self,
        connection: Connection,
        refs: set[tuple[str, UUID]],
    ) -> list[dict[str, Any]]:
        """Return references from rows outside refs to rows inside refs."""

        result: list[dict[str, Any]] = []
        refs_by_table = _group_refs(refs)
        for document_id in refs_by_table.get("documents", ()):
            result.extend(
                _external_fk_rows(
                    connection,
                    "chapters",
                    "document_id",
                    document_id,
                    refs,
                )
            )
            result.extend(_external_fk_rows(connection, "paragraphs", "document_id", document_id, refs))
            result.extend(_external_fk_rows(connection, "semantic_chunks", "document_id", document_id, refs))
        for chapter_id in refs_by_table.get("chapters", ()):
            result.extend(_external_fk_rows(connection, "paragraphs", "chapter_id", chapter_id, refs))
            result.extend(_external_fk_rows(connection, "semantic_chunks", "chapter_id", chapter_id, refs))
        for paragraph_id in refs_by_table.get("paragraphs", ()):
            result.extend(_external_fk_rows(connection, "semantic_chunks", "paragraph_id", paragraph_id, refs))
        for chunk_id in refs_by_table.get("semantic_chunks", ()):
            rows = connection.execute(
                text(
                    "SELECT source_chunk_uuid::text AS id, relation_type "
                    "FROM semantic_chunk_links "
                    "WHERE target_chunk_uuid = :target"
                ),
                {"target": chunk_id},
            ).mappings().all()
            for row in rows:
                source = UUID(str(row["id"]))
                if ("semantic_chunks", source) not in refs:
                    result.append(
                        {
                            "from_table": "semantic_chunk_links",
                            "from_id": str(source),
                            "from_column": "target_chunk_uuid",
                            "to_table": "semantic_chunks",
                            "to_id": str(chunk_id),
                        }
                    )
        return result

    def _mark_deleted(self, *, entity_type: str, ids: Sequence[str], deleted: bool) -> dict[str, Any]:
        table = _entity_table(entity_type)
        roots = self._roots_for(table, ids)
        if not roots:
            return {"outcome": "updated", "updated": {}, "is_deleted": deleted}
        with self._transaction() as connection:
            closure = self._closure(connection, roots)
            updated = self._set_deleted(connection, closure, deleted)
        return {"outcome": "updated", "updated": updated, "is_deleted": deleted}

    def _closure(self, connection: Connection, roots: Sequence[EntityRef]) -> set[tuple[str, UUID]]:
        refs = {root.as_key() for root in roots}
        changed = True
        while changed:
            changed = False
            grouped = _group_refs(refs)
            for document_id in tuple(grouped.get("documents", ())):
                changed |= _add_children(connection, refs, "chapters", "document_id", document_id)
                changed |= _add_children(connection, refs, "paragraphs", "document_id", document_id)
                changed |= _add_children(connection, refs, "semantic_chunks", "document_id", document_id)
            for chapter_id in tuple(grouped.get("chapters", ())):
                changed |= _add_children(connection, refs, "paragraphs", "chapter_id", chapter_id)
                changed |= _add_children(connection, refs, "semantic_chunks", "chapter_id", chapter_id)
            for paragraph_id in tuple(grouped.get("paragraphs", ())):
                changed |= _add_children(connection, refs, "semantic_chunks", "paragraph_id", paragraph_id)
        return refs

    def _roots_for(self, table: str, ids: Sequence[str]) -> list[EntityRef]:
        if table != "projects":
            return [EntityRef(table, UUID(item)) for item in ids]
        with self._connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id FROM documents "
                    "WHERE block_meta ->> 'project' = ANY(CAST(:projects AS text[]))"
                ),
                {"projects": [str(item) for item in ids]},
            ).scalars().all()
        return [EntityRef("documents", row) for row in rows]

    def _list_projects(self, *, limit: int, offset: int, show_deleted: bool) -> dict[str, Any]:
        where = ["block_meta ? 'project'"]
        if not show_deleted:
            where.append("is_deleted IS FALSE")
        sql = (
            "SELECT block_meta ->> 'project' AS project, count(*)::int AS document_count "
            f"FROM documents WHERE {' AND '.join(where)} "
            "GROUP BY block_meta ->> 'project' ORDER BY project ASC LIMIT :limit OFFSET :offset"
        )
        count_sql = (
            "SELECT count(*) FROM ("
            "SELECT block_meta ->> 'project' AS project FROM documents "
            f"WHERE {' AND '.join(where)} GROUP BY block_meta ->> 'project'"
            ") AS projects"
        )
        params = {"limit": _limit(limit), "offset": _offset(offset)}
        with self._connect() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
            total = int(connection.execute(text(count_sql), params).scalar_one())
        return {
            "entity_type": "projects",
            "items": [_json_row(row) for row in rows],
            "limit": params["limit"],
            "offset": params["offset"],
            "total": total,
            "show_deleted": show_deleted,
        }

    def _set_deleted(
        self,
        connection: Connection,
        refs: set[tuple[str, UUID]],
        deleted: bool,
    ) -> dict[str, int]:
        updated: dict[str, int] = {}
        for table, ids in _group_refs(refs).items():
            timestamp = "now()" if deleted else "NULL"
            count = connection.execute(
                text(
                    f"UPDATE {table} SET is_deleted = :deleted, deleted_at = {timestamp} "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"deleted": deleted, "ids": [str(item) for item in ids]},
            ).rowcount
            updated[table] = int(count or 0)
        return updated

    def _delete_closure(self, connection: Connection, refs: set[tuple[str, UUID]]) -> dict[str, int]:
        deleted: dict[str, int] = {}
        grouped = _group_refs(refs)
        chunk_ids = grouped.get("semantic_chunks")
        if chunk_ids:
            link_count = connection.execute(
                text(
                    "DELETE FROM semantic_chunk_links "
                    "WHERE source_chunk_uuid = ANY(CAST(:ids AS uuid[])) "
                    "OR target_chunk_uuid = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(item) for item in chunk_ids]},
            ).rowcount
            if link_count:
                deleted["semantic_chunk_links"] = int(link_count)
        for table in ("semantic_chunks", "paragraphs", "chapters", "documents"):
            ids = grouped.get(table)
            if not ids:
                continue
            count = connection.execute(
                text(f"DELETE FROM {table} WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": [str(item) for item in ids]},
            ).rowcount
            deleted[table] = int(count or 0)
        return deleted

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

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        engine = self._engine()
        try:
            with engine.begin() as connection:
                yield connection
        finally:
            engine.dispose()


def installed_entity_lifecycle_service(config: Mapping[str, Any] | None = None) -> EntityLifecycleService | None:
    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return EntityLifecycleService(database_url)


def _entity_table(entity_type: str) -> str:
    key = entity_type.strip().lower()
    try:
        return ENTITY_TABLES[key]
    except KeyError as exc:
        raise ValueError(f"unsupported entity_type: {entity_type}") from exc


def _validated_fields(table: str, fields: Sequence[str] | None) -> tuple[str, ...]:
    allowed = _allowed_fields(table)
    selected = tuple(fields or DEFAULT_FIELDS[table])
    unknown = sorted(set(selected) - allowed)
    if unknown:
        raise ValueError(f"unsupported fields for {table}: {', '.join(unknown)}")
    return selected


def _allowed_fields(table: str) -> set[str]:
    return set(DEFAULT_FIELDS[table]) | {
        "created_at",
        "updated_at",
        "deleted_at",
        "source_version",
        "source_path",
        "source_name",
        "source_hash",
        "language",
        "quality_score",
        "search_weight",
        "char_count",
        "chunk_type",
        "score",
        "source_start",
        "source_end",
        "level",
    }


def _filters_sql(table: str, filters: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    allowed = _allowed_fields(table) | {
        "id",
        "document_id",
        "chapter_id",
        "paragraph_id",
        "is_deleted",
    }
    where: list[str] = []
    params: dict[str, Any] = {}
    for index, (key, value) in enumerate(filters.items()):
        param = f"filter_{index}"
        if key.startswith("block_meta."):
            meta_key = key.split(".", 1)[1]
            where.append(f"block_meta ->> :{param}_key = :{param}")
            params[f"{param}_key"] = meta_key
            params[param] = str(value)
        elif key in allowed:
            where.append(f"{key} = :{param}")
            params[param] = value
        else:
            raise ValueError(f"unsupported filter for {table}: {key}")
    return where, params


def _limit(value: int) -> int:
    if value < 1 or value > 500:
        raise ValueError("limit must be between 1 and 500")
    return value


def _offset(value: int) -> int:
    if value < 0:
        raise ValueError("offset must be non-negative")
    return value


def _group_refs(refs: Iterable[tuple[str, UUID]]) -> dict[str, list[UUID]]:
    grouped: dict[str, list[UUID]] = {}
    for table, entity_id in refs:
        grouped.setdefault(table, []).append(entity_id)
    return grouped


def _add_children(
    connection: Connection,
    refs: set[tuple[str, UUID]],
    table: str,
    column: str,
    parent_id: UUID,
) -> bool:
    rows = connection.execute(
        text(f"SELECT id FROM {table} WHERE {column} = :parent_id"),
        {"parent_id": parent_id},
    ).scalars().all()
    changed = False
    for row in rows:
        key = (table, row)
        if key not in refs:
            refs.add(key)
            changed = True
    return changed


def _external_fk_rows(
    connection: Connection,
    table: str,
    column: str,
    target_id: UUID,
    deleting: set[tuple[str, UUID]],
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(f"SELECT id FROM {table} WHERE {column} = :target_id"),
        {"target_id": target_id},
    ).scalars().all()
    return [
        {
            "from_table": table,
            "from_id": str(row),
            "from_column": column,
            "to_id": str(target_id),
        }
        for row in rows
        if (table, row) not in deleting
    ]


def _json_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif isinstance(value, tuple):
            result[key] = list(value)
        else:
            result[key] = value
    return result


__all__ = [
    "DeletionSafetyError",
    "ENTITY_TABLES",
    "EntityLifecycleService",
    "installed_entity_lifecycle_service",
]
