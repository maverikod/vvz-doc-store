"""Commands for listing and managing semantic chunk text versions."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.commands.result import ErrorResult
from mcp_proxy_adapter.core.errors import ValidationError

from doc_store_server.commands.validation import parse_uuid4
from doc_store_server.runtime.chunk_versions import (
    LAST_VERSION_DELETE_CODE,
    ChunkTextVersionError,
    installed_chunk_text_version_service,
)


class ChunkVersionBoundary(Protocol):
    """Runtime boundary used by chunk text version commands."""

    def list_versions(self, *, chunk_id: str) -> Mapping[str, Any]: ...

    def set_current(self, *, chunk_id: str, version_no: int) -> Mapping[str, Any]: ...

    def delete_version(self, *, chunk_id: str, version_no: int) -> Mapping[str, Any]: ...


_MAX_LIMIT = 1000
_MAX_OFFSET = 10_000_000


class _ChunkVersionCommand(Command):
    """Shared validation, metadata, and error mapping for version commands."""

    version: ClassVar[str] = "0.1.0"
    category: ClassVar[str] = "doc-store.chunk-versions"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False
    chunk_version_boundary: ClassVar[ChunkVersionBoundary | None] = None
    descr: ClassVar[str]
    detailed_description: ClassVar[str]
    schema_properties: ClassVar[dict[str, dict[str, Any]]]
    required_fields: ClassVar[tuple[str, ...]]
    parameter_docs: ClassVar[dict[str, dict[str, Any]]]
    return_contract: ClassVar[dict[str, Any]]
    usage_examples: ClassVar[list[dict[str, Any]]]

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {key: dict(value) for key, value in cls.schema_properties.items()},
            "required": list(cls.required_fields),
            "additionalProperties": False,
        }

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "version": cls.version,
            "description": cls.descr,
            "category": cls.category,
            "author": cls.author,
            "email": cls.email,
            "detailed_description": cls.detailed_description,
            "parameters": cls.parameter_docs,
            "return_value": cls.return_contract,
            "usage_examples": cls.usage_examples,
            "error_cases": {
                "INVALID_PARAMS": "Malformed UUID4, non-positive version number, or invalid limit/offset.",
                "NOT_FOUND": "The semantic chunk or requested text version does not exist.",
                "LAST_VERSION_DELETE_FORBIDDEN": "The last text version cannot be deleted; delete the chunk instead.",
                "VERSION_BOUNDARY_UNAVAILABLE": "The chunk text version runtime boundary is not configured.",
                "INTERNAL_ERROR": "The chunk text version operation failed unexpectedly.",
            },
            "best_practices": [
                "Use UUID4 chunk identifiers returned by the canonical chunk boundary.",
                "Treat version_no as an opaque positive integer and select the current version explicitly when needed.",
                "Do not delete the last version; delete the owning chunk when its text is no longer needed.",
            ],
        }

    def _boundary(self, context: Mapping[str, Any] | None) -> ChunkVersionBoundary | None:
        if context:
            boundary = context.get("chunk_version_boundary") or context.get("chunk_text_version_boundary")
            if boundary is not None:
                return boundary
        return self.chunk_version_boundary or installed_chunk_text_version_service()

    def _error(self, message: str, code: str) -> ErrorResult:
        return ErrorResult(f"{code}: {message}", details={"code": code})

    def _parse_chunk_id(self, value: Any) -> str:
        return str(parse_uuid4(value, "chunk_id", self.name))

    @staticmethod
    def _parse_version(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("version_no must be a positive integer")
        return value

    @staticmethod
    def _parse_limit(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
            raise ValueError(f"limit must be an integer between 1 and {_MAX_LIMIT}")
        return value

    @staticmethod
    def _parse_offset(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_OFFSET:
            raise ValueError(f"offset must be an integer between 0 and {_MAX_OFFSET}")
        return value

    def _map_exception(self, exc: Exception) -> ErrorResult:
        if isinstance(exc, ChunkTextVersionError):
            if exc.code == "VERSION_NOT_FOUND":
                return self._error(str(exc), "NOT_FOUND")
            if exc.code == LAST_VERSION_DELETE_CODE:
                return self._error(str(exc), "LAST_VERSION_DELETE_FORBIDDEN")
            return self._error(str(exc), "INTERNAL_ERROR")
        if isinstance(exc, LookupError):
            return self._error(str(exc), "NOT_FOUND")
        if isinstance(exc, ValueError):
            return self._error(str(exc), "INVALID_PARAMS")
        if isinstance(exc, ValidationError):
            return self._error(str(exc), "INVALID_PARAMS")
        return self._error(str(exc), "INTERNAL_ERROR")


class ChunkVersionListCommand(_ChunkVersionCommand):
    """List version summaries for one semantic chunk."""

    name = "chunk_version_list"
    descr = "List semantic chunk text versions with current-state and checksum metadata."
    detailed_description = "Validates a UUID4 chunk identifier and pagination, then delegates version listing to the runtime boundary."
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_LIMIT,
            "default": 100,
            "description": "Maximum versions returned in this page (1-1000).",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "maximum": _MAX_OFFSET,
            "default": 0,
            "description": "Zero-based offset into the version list (0-10000000).",
        },
    }
    required_fields = ("chunk_id",)
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "limit": {"type": "integer", "description": "Maximum versions returned (1-1000).", "required": False},
        "offset": {"type": "integer", "description": "Zero-based version offset (0-10000000).", "required": False},
    }
    return_contract = {"description": "Chunk id, total version count, and version summary items."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000"}, {"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "limit": 20, "offset": 20}]

    async def execute(self, chunk_id: Any, limit: int = 100, offset: int = 0, context: Mapping[str, Any] | None = None) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_limit = self._parse_limit(limit)
            parsed_offset = self._parse_offset(offset)
        except Exception as exc:
            return self._map_exception(exc)
        boundary = self._boundary(context)
        if boundary is None or not hasattr(boundary, "list_versions"):
            return self._error("chunk text version boundary is unavailable", "VERSION_BOUNDARY_UNAVAILABLE")
        try:
            result = boundary.list_versions(chunk_id=parsed_id)
            if inspect.isawaitable(result):
                result = await result
            payload = dict(result)
            items = [_summary(item) for item in payload.get("items", [])]
            payload["items"] = items[parsed_offset : parsed_offset + parsed_limit]
            payload.setdefault("total", len(items))
            payload["offset"] = parsed_offset
            payload["limit"] = parsed_limit
            return CommandResult(data=payload)
        except Exception as exc:
            return self._map_exception(exc)


class ChunkVersionSetCurrentCommand(_ChunkVersionCommand):
    """Make an existing semantic chunk text version current."""

    name = "chunk_version_set_current"
    descr = "Set the current semantic chunk text version."
    detailed_description = "Validates a UUID4 chunk identifier and positive version number, then updates the current version projection through the runtime boundary."
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "version_no": {"type": "integer", "minimum": 1, "description": "Positive version number."},
    }
    required_fields = ("chunk_id", "version_no")
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "version_no": {"type": "integer", "description": "Positive version number.", "required": True},
    }
    return_contract = {"description": "Chunk id, set-current outcome, and selected version summary."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "version_no": 2}]

    async def execute(self, chunk_id: Any, version_no: Any, context: Mapping[str, Any] | None = None) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_version = self._parse_version(version_no)
        except Exception as exc:
            return self._map_exception(exc)
        boundary = self._boundary(context)
        if boundary is None or not hasattr(boundary, "set_current"):
            return self._error("chunk text version boundary is unavailable", "VERSION_BOUNDARY_UNAVAILABLE")
        try:
            result = boundary.set_current(chunk_id=parsed_id, version_no=parsed_version)
            if inspect.isawaitable(result):
                result = await result
            return CommandResult(data=dict(result))
        except Exception as exc:
            return self._map_exception(exc)


class ChunkVersionDeleteCommand(_ChunkVersionCommand):
    """Delete one semantic chunk text version."""

    name = "chunk_version_delete"
    descr = "Delete a semantic chunk text version while preserving the last-version guard."
    detailed_description = "Validates a UUID4 chunk identifier and positive version number, then deletes through the runtime boundary."
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "version_no": {"type": "integer", "minimum": 1, "description": "Positive version number."},
    }
    required_fields = ("chunk_id", "version_no")
    parameter_docs = ChunkVersionSetCurrentCommand.parameter_docs
    return_contract = {"description": "Chunk id, deleted version number, and resulting current version number."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "version_no": 1}]

    async def execute(self, chunk_id: Any, version_no: Any, context: Mapping[str, Any] | None = None) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_version = self._parse_version(version_no)
        except Exception as exc:
            return self._map_exception(exc)
        boundary = self._boundary(context)
        if boundary is None or not hasattr(boundary, "delete_version"):
            return self._error("chunk text version boundary is unavailable", "VERSION_BOUNDARY_UNAVAILABLE")
        try:
            result = boundary.delete_version(chunk_id=parsed_id, version_no=parsed_version)
            if inspect.isawaitable(result):
                result = await result
            return CommandResult(data=dict(result))
        except Exception as exc:
            return self._map_exception(exc)


def _summary(item: Mapping[str, Any]) -> dict[str, Any]:
    """Expose stable list fields without returning the stored text body."""

    digest = item.get("text_sha256") or item.get("checksum")
    comment = item.get("comment")
    if comment is None and isinstance(item.get("block_meta"), Mapping):
        comment = item["block_meta"].get("comment")
    text_value = item.get("text")
    preview = item.get("preview")
    if preview is None and isinstance(text_value, str):
        preview = text_value[:240]
    created_at = item.get("created_at")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    return {
        "version_no": item.get("version_no"),
        "preview": preview or "",
        "created_at": created_at,
        "current": bool(item.get("current", item.get("is_current", False))),
        "char_count": item.get("char_count", len(text_value) if isinstance(text_value, str) else 0),
        "checksum": digest,
        "text_sha256": digest,
        "comment": comment,
    }


__all__ = [
    "ChunkVersionBoundary",
    "ChunkVersionDeleteCommand",
    "ChunkVersionListCommand",
    "ChunkVersionSetCurrentCommand",
]
