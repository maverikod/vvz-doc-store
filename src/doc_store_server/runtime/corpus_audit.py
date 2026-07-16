"""First-class corpus audit queries over indexed documents and chunks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import os
import re
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.previews import chunk_preview
from doc_store_server.runtime.semantic_relations import unit_title_edit_capabilities


DEFAULT_CORRECTION_MARKERS = (
    "корректировка",
    "правка",
    "уточнение",
    "заменить",
    "заменяем",
    "исправление",
    "сведение",
    "канон",
    "реструктуризация",
    "дополнение",
)
DEFAULT_CONFLICT_MARKERS = (
    "противоречие",
    "конфликт",
    "несогласованность",
    "неверно",
    "ошибка",
    "дыра",
    "отсутствует",
    "не хватает",
    "не оформлена",
)
_IDENTIFIER_RE = re.compile(r"\b(?P<identifier>7d-(?P<number>\d+)(?:-[\w.-]+)?)", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$")


@dataclass(frozen=True, slots=True)
class AuditScope:
    project: str | None
    document_id: str | None
    source_name: str | None
    seven_d_number: int | None
    include_deleted: bool


class CorpusAuditService:
    """Read-only analysis commands over the indexed corpus."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    def audit(
        self,
        *,
        mode: str = "inventory",
        project: str | None = None,
        document_id: str | None = None,
        source_name: str | None = None,
        seven_d_number: int | None = None,
        markers: Sequence[str] | None = None,
        min_length: int = 80,
        include_aggregators: bool = False,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        mode = _choice(
            mode,
            {
                "inventory",
                "corrections",
                "conflicts",
                "exact_duplicates",
                "topics",
                "unit_title_capabilities",
            },
            "mode",
        )
        limit = _bounded_int(limit, "limit", 1, 500)
        offset = _bounded_int(offset, "offset", 0, 100000)
        min_length = _bounded_int(min_length, "min_length", 1, 100000)
        scope = AuditScope(project, document_id, source_name, seven_d_number, include_deleted)

        if mode == "unit_title_capabilities":
            return {
                "status": "ok",
                "mode": mode,
                "scope": asdict(scope),
                "items": [],
                "groups": [],
                "diagnostics": {"unit_title_editing": unit_title_edit_capabilities()},
                "pagination": {"limit": limit, "offset": offset, "total": 0},
            }
        with self._connect() as connection:
            if mode == "inventory":
                payload = self._inventory(connection, scope=scope, limit=limit, offset=offset)
            elif mode == "corrections":
                payload = self._marker_items(
                    connection,
                    scope=scope,
                    markers=tuple(markers or DEFAULT_CORRECTION_MARKERS),
                    mode=mode,
                    limit=limit,
                    offset=offset,
                )
            elif mode == "conflicts":
                payload = self._marker_items(
                    connection,
                    scope=scope,
                    markers=tuple(markers or DEFAULT_CONFLICT_MARKERS),
                    mode=mode,
                    limit=limit,
                    offset=offset,
                )
            elif mode == "exact_duplicates":
                payload = self._duplicates(
                    connection,
                    scope=scope,
                    min_length=min_length,
                    include_aggregators=include_aggregators,
                    limit=limit,
                    offset=offset,
                )
            else:
                payload = self._topics(connection, scope=scope, limit=limit, offset=offset)
        payload.setdefault("diagnostics", {})
        payload["diagnostics"]["unit_title_editing"] = unit_title_edit_capabilities()
        return {"status": "ok", "mode": mode, "scope": asdict(scope), **payload}

    def _inventory(
        self,
        connection: Connection,
        *,
        scope: AuditScope,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        where, params = _scope_where(scope, prefix="d")
        sql = f"""
            SELECT d.id::text AS document_id,
                   d.source_name,
                   d.source_path,
                   d.title,
                   d.block_meta,
                   d.created_at,
                   count(DISTINCT sc.id) AS chunk_count,
                   count(DISTINCT sce.chunk_uuid) FILTER (WHERE sce.active IS TRUE) AS vectorized_chunk_count,
                   min(sc.order_index) AS first_order_index,
                   (array_agg(sc.text ORDER BY sc.order_index ASC, sc.id ASC))[1] AS first_preview
            FROM documents AS d
            LEFT JOIN semantic_chunks AS sc ON sc.document_id = d.id AND (:include_deleted OR sc.deleted_at IS NULL)
            LEFT JOIN semantic_chunk_embeddings AS sce ON sce.chunk_uuid = sc.id AND sce.active IS TRUE
            WHERE {' AND '.join(where)}
            GROUP BY d.id
            ORDER BY d.created_at ASC, d.id ASC
        """
        rows = connection.execute(text(sql), {**params, "include_deleted": scope.include_deleted}).mappings().all()
        items = [_inventory_item(row) for row in rows]
        issues = _inventory_issues(items)
        return {
            "items": items[offset: offset + limit],
            "groups": [],
            "issues": issues,
            "pagination": {"limit": limit, "offset": offset, "total": len(items)},
            "diagnostics": {"parsed_identifier_count": sum(1 for item in items if item["parsed_identifier"])},
        }

    def _marker_items(
        self,
        connection: Connection,
        *,
        scope: AuditScope,
        markers: Sequence[str],
        mode: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        if not markers:
            raise ValueError("markers must not be empty")
        where, params = _scope_where(scope, prefix="d")
        marker_clauses: list[str] = []
        for index, marker in enumerate(markers):
            key = f"marker_{index}"
            params[key] = f"%{marker}%"
            marker_clauses.append(f"sc.text ILIKE :{key}")
        where.append("(" + " OR ".join(marker_clauses) + ")")
        if not scope.include_deleted:
            where.append("sc.deleted_at IS NULL")
        sql = f"""
            SELECT sc.id::text AS chunk_id,
                   sc.document_id::text AS document_id,
                   sc.paragraph_id::text AS paragraph_id,
                   sc.order_index,
                   sc.text,
                   d.source_name,
                   d.title,
                   d.block_meta
            FROM semantic_chunks AS sc
            JOIN documents AS d ON d.id = sc.document_id
            WHERE {' AND '.join(where)}
            ORDER BY d.created_at ASC, sc.order_index ASC, sc.id ASC
        """
        rows = connection.execute(text(sql), params).mappings().all()
        items = [_marker_item(row, markers) for row in rows]
        groups = _conflict_groups(items) if mode == "conflicts" else []
        return {
            "items": items[offset: offset + limit],
            "groups": groups[offset: offset + limit] if groups else [],
            "pagination": {"limit": limit, "offset": offset, "total": len(groups) if groups else len(items)},
            "diagnostics": {"markers": list(markers), "match_count": len(items)},
        }

    def _duplicates(
        self,
        connection: Connection,
        *,
        scope: AuditScope,
        min_length: int,
        include_aggregators: bool,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        where, params = _scope_where(scope, prefix="d")
        where.append("length(sc.text) >= :min_length")
        params["min_length"] = min_length
        if not scope.include_deleted:
            where.append("sc.deleted_at IS NULL")
        sql = f"""
            SELECT sc.id::text AS chunk_id,
                   sc.document_id::text AS document_id,
                   sc.paragraph_id::text AS paragraph_id,
                   sc.order_index,
                   sc.text,
                   d.source_name,
                   d.title
            FROM semantic_chunks AS sc
            JOIN documents AS d ON d.id = sc.document_id
            WHERE {' AND '.join(where)}
            ORDER BY d.created_at ASC, sc.order_index ASC, sc.id ASC
        """
        rows = connection.execute(text(sql), params).mappings().all()
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            source = row.get("source_name")
            if not include_aggregators and isinstance(source, str) and re.match(r"7d-0+\D", source, re.IGNORECASE):
                continue
            normalized = _normalize_text(str(row["text"]))
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            buckets.setdefault(digest, []).append(_duplicate_item(row, normalized))
        groups = [
            {
                "group_id": digest,
                "hash": digest,
                "count": len(items),
                "document_count": len({item["document_id"] for item in items}),
                "preview": items[0]["preview"],
                "items": items,
            }
            for digest, items in sorted(buckets.items())
            if len(items) > 1
        ]
        return {
            "items": [],
            "groups": groups[offset: offset + limit],
            "pagination": {"limit": limit, "offset": offset, "total": len(groups)},
            "diagnostics": {"min_length": min_length, "include_aggregators": include_aggregators},
        }

    def _topics(
        self,
        connection: Connection,
        *,
        scope: AuditScope,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        where, params = _scope_where(scope, prefix="d")
        sql = f"""
            SELECT d.id::text AS document_id,
                   d.source_name,
                   d.title,
                   (array_agg(sc.text ORDER BY sc.order_index ASC, sc.id ASC))[1:8] AS previews,
                   count(sc.id) AS chunk_count
            FROM documents AS d
            LEFT JOIN semantic_chunks AS sc ON sc.document_id = d.id AND (:include_deleted OR sc.deleted_at IS NULL)
            WHERE {' AND '.join(where)}
            GROUP BY d.id
            ORDER BY d.created_at ASC, d.id ASC
        """
        rows = connection.execute(text(sql), {**params, "include_deleted": scope.include_deleted}).mappings().all()
        items = [_topic_item(row) for row in rows]
        return {
            "items": items[offset: offset + limit],
            "groups": [],
            "pagination": {"limit": limit, "offset": offset, "total": len(items)},
            "diagnostics": {"topic_source": "document title, source_name, markdown headings, first previews"},
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


def installed_corpus_audit_service(config: Mapping[str, Any] | None = None) -> CorpusAuditService | None:
    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return CorpusAuditService(database_url)


def _scope_where(scope: AuditScope, *, prefix: str) -> tuple[list[str], dict[str, Any]]:
    where = ["TRUE"]
    params: dict[str, Any] = {}
    if not scope.include_deleted:
        where.append(f"{prefix}.deleted_at IS NULL")
    if scope.project is not None:
        params["project"] = scope.project
        where.append(f"({prefix}.block_meta ->> 'project' = :project OR {prefix}.block_meta ->> 'project_id' = :project)")
    if scope.document_id is not None:
        params["document_id"] = scope.document_id
        where.append(f"{prefix}.id = CAST(:document_id AS uuid)")
    if scope.source_name is not None:
        params["source_name"] = scope.source_name
        where.append(f"({prefix}.source_name = :source_name OR {prefix}.source_path = :source_name)")
    if scope.seven_d_number is not None:
        params["seven_d_number"] = f"7d-{scope.seven_d_number:02d}"
        where.append(f"{prefix}.source_name ILIKE :seven_d_number || '%'")
    return where, params


def _inventory_item(row: Mapping[str, Any]) -> dict[str, Any]:
    source_name = row.get("source_name")
    first_preview = str(row.get("first_preview") or "")
    parsed_identifier, parsed_number = _parse_identifier(source_name, first_preview, row.get("title"))
    metadata_number = _metadata_7d_number(row.get("block_meta"))
    return {
        "document_id": row["document_id"],
        "source_name": source_name,
        "source_title": row.get("title"),
        "parsed_identifier": parsed_identifier,
        "parsed_7d_number": parsed_number,
        "metadata_7d_number": metadata_number,
        "chunk_count": int(row.get("chunk_count") or 0),
        "vectorized_chunk_count": int(row.get("vectorized_chunk_count") or 0),
        "first_preview": _preview(first_preview),
    }


def _inventory_issues(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    by_number: dict[int, list[Mapping[str, Any]]] = {}
    for item in items:
        number = item.get("parsed_7d_number")
        if isinstance(number, int):
            by_number.setdefault(number, []).append(item)
        metadata_number = item.get("metadata_7d_number")
        if metadata_number is not None and number is not None and metadata_number != number:
            issues.append(
                {
                    "kind": "metadata_mismatch",
                    "document_id": item["document_id"],
                    "parsed_7d_number": number,
                    "metadata_7d_number": metadata_number,
                }
            )
    for number, rows in sorted(by_number.items()):
        if len(rows) > 1:
            issues.append(
                {
                    "kind": "duplicate_7d_number",
                    "7d_number": number,
                    "document_ids": [row["document_id"] for row in rows],
                }
            )
    if by_number:
        numbers = sorted(by_number)
        missing = [number for number in range(numbers[0], numbers[-1] + 1) if number not in by_number]
        if missing:
            issues.append({"kind": "missing_7d_numbers", "numbers": missing})
    ordered_numbers = [item.get("parsed_7d_number") for item in items if isinstance(item.get("parsed_7d_number"), int)]
    if ordered_numbers != sorted(ordered_numbers):
        issues.append({"kind": "non_monotonic_order", "observed": ordered_numbers})
    return issues


def _marker_item(row: Mapping[str, Any], markers: Sequence[str]) -> dict[str, Any]:
    text_value = str(row["text"])
    matched = [marker for marker in markers if marker.lower() in text_value.lower()]
    identifier, number = _parse_identifier(row.get("source_name"), text_value, row.get("title"))
    return {
        "id": row["chunk_id"],
        "document_id": row["document_id"],
        "chunk_id": row["chunk_id"],
        "paragraph_id": row["paragraph_id"],
        "source_name": row.get("source_name"),
        "7d_number": number,
        "parsed_identifier": identifier,
        "paragraph_number": row.get("order_index"),
        "matched_marker": matched[0] if matched else None,
        "markers": matched,
        "preview": _preview(text_value),
        "score": len(matched),
    }


def _conflict_groups(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        key = str(item.get("matched_marker") or "conflict")
        buckets.setdefault(key, []).append(item)
    return [
        {
            "group_id": f"conflict-marker-{index:03d}",
            "reason": f"shared conflict marker: {marker}",
            "item_count": len(rows),
            "items": list(rows),
        }
        for index, (marker, rows) in enumerate(sorted(buckets.items()), 1)
    ]


def _duplicate_item(row: Mapping[str, Any], normalized: str) -> dict[str, Any]:
    identifier, number = _parse_identifier(row.get("source_name"), row.get("text"), row.get("title"))
    return {
        "chunk_id": row["chunk_id"],
        "document_id": row["document_id"],
        "paragraph_id": row["paragraph_id"],
        "source_name": row.get("source_name"),
        "7d_number": number,
        "parsed_identifier": identifier,
        "paragraph_number": row.get("order_index"),
        "preview": _preview(normalized),
    }


def _topic_item(row: Mapping[str, Any]) -> dict[str, Any]:
    previews = [str(item) for item in (row.get("previews") or []) if item]
    headings = []
    for preview in previews:
        match = _HEADING_RE.match(preview)
        if match:
            headings.append(match.group("title"))
    identifier, number = _parse_identifier(row.get("source_name"), row.get("title"), *previews)
    return {
        "document_id": row["document_id"],
        "source_name": row.get("source_name"),
        "7d_number": number,
        "parsed_identifier": identifier,
        "title": row.get("title"),
        "chunk_count": int(row.get("chunk_count") or 0),
        "headings": headings[:10],
        "preview_topics": [_preview(item, limit=120) for item in previews[:5]],
    }


def _parse_identifier(*values: Any) -> tuple[str | None, int | None]:
    for value in values:
        if not isinstance(value, str):
            continue
        match = _IDENTIFIER_RE.search(value)
        if match:
            return match.group("identifier"), int(match.group("number"))
    return None, None


def _metadata_7d_number(value: Any) -> int | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("7d_number", "seven_d_number", "parsed_7d_number"):
        if key in value and value[key] is not None:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                return None
    return None


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _preview(value: str, *, limit: int = 220) -> str:
    return chunk_preview(value, limit=limit)


def _choice(value: str, allowed: set[str], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return value


def _bounded_int(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


__all__ = [
    "CorpusAuditService",
    "DEFAULT_CONFLICT_MARKERS",
    "DEFAULT_CORRECTION_MARKERS",
    "installed_corpus_audit_service",
]
