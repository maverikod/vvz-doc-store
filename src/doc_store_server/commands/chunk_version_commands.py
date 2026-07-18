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
    CURRENT_VERSION_MISMATCH_CODE,
    CURRENT_VERSION_RETIRE_CODE,
    LAST_VERSION_DELETE_CODE,
    ChunkTextVersionError,
    installed_chunk_text_version_service,
)


class ChunkVersionBoundary(Protocol):
    """Runtime boundary used by chunk text version commands."""

    def list_versions(self, *, chunk_id: str, include_deleted: bool = False) -> Mapping[str, Any]: ...

    def history(self, *, chunk_id: str, include_deleted: bool = False) -> Mapping[str, Any]: ...

    def get_version(
        self,
        *,
        chunk_id: str,
        version_no: int | None = None,
        current: bool = False,
        include_text: bool = True,
    ) -> Mapping[str, Any]: ...

    def append_version(
        self,
        *,
        chunk_id: str,
        text_value: str,
        comment: str | None = None,
        actor: str | None = None,
        expected_current_version: int | None = None,
        operation_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def update_text(
        self,
        *,
        chunk_id: str,
        text_value: str,
        comment: str | None = None,
        actor: str | None = None,
        expected_current_version: int | None = None,
        operation_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def set_current(
        self,
        *,
        chunk_id: str,
        version_no: int,
        comment: str | None = None,
        actor: str | None = None,
    ) -> Mapping[str, Any]: ...

    def restore_version(
        self,
        *,
        chunk_id: str,
        version_no: int,
        comment: str | None = None,
        actor: str | None = None,
        expected_current_version: int | None = None,
        operation_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def retire_version(
        self,
        *,
        chunk_id: str,
        version_no: int,
        replacement_version_no: int | None = None,
        comment: str | None = None,
        actor: str | None = None,
    ) -> Mapping[str, Any]: ...

    def delete_version(self, *, chunk_id: str, version_no: int) -> Mapping[str, Any]: ...

    def diff_versions(
        self,
        *,
        chunk_id: str,
        from_version_no: int,
        to_version_no: int,
        context_lines: int = 3,
    ) -> Mapping[str, Any]: ...


_MAX_LIMIT = 1000
_MAX_OFFSET = 10_000_000


class _ChunkVersionCommand(Command):
    """Shared validation, metadata, and error mapping for version commands."""

    version: ClassVar[str] = "0.2.0"
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
                "INVALID_PARAMS": "Malformed UUID4, non-positive version number, invalid pagination, or invalid option value.",
                "NOT_FOUND": "The semantic chunk or requested text version does not exist.",
                "LAST_VERSION_DELETE_FORBIDDEN": "The last text version cannot be removed; delete the chunk instead.",
                "CURRENT_VERSION_RETIRE_REQUIRES_REPLACEMENT": "Retiring the active version requires replacement_version_no.",
                "CURRENT_VERSION_MISMATCH": "expected_current_version was supplied and no longer matches the active version.",
                "VERSION_BOUNDARY_UNAVAILABLE": "The chunk text version runtime boundary is not configured.",
                "INTERNAL_ERROR": "The chunk text version operation failed unexpectedly.",
            },
            "best_practices": [
                "Use UUID4 chunk identifiers returned by document ingestion, retrieval, or search.",
                "Use expected_current_version on add/update/restore when concurrent edits are possible.",
                "Use chunk_version_retire for lifecycle removal; chunk_version_delete is the hard-delete administrative path.",
                "Read commands return previews by default; chunk_version_get can include the full version text.",
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

    def _parse_operation_id(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(parse_uuid4(value, "operation_id", self.name))

    @staticmethod
    def _parse_text(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("text must be a string")
        return value

    @staticmethod
    def _parse_bool(value: Any, field_name: str) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{field_name} must be a boolean")
        return value

    @staticmethod
    def _parse_version(value: Any, field_name: str = "version_no") -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field_name} must be a positive integer")
        return value

    @classmethod
    def _parse_optional_version(cls, value: Any, field_name: str) -> int | None:
        if value is None:
            return None
        return cls._parse_version(value, field_name)

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

    @staticmethod
    def _parse_context_lines(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
            raise ValueError("context_lines must be an integer between 0 and 20")
        return value

    def _map_exception(self, exc: Exception) -> ErrorResult:
        if isinstance(exc, ChunkTextVersionError):
            if exc.code == "VERSION_NOT_FOUND":
                return self._error(str(exc), "NOT_FOUND")
            if exc.code == LAST_VERSION_DELETE_CODE:
                return self._error(str(exc), "LAST_VERSION_DELETE_FORBIDDEN")
            if exc.code == CURRENT_VERSION_RETIRE_CODE:
                return self._error(str(exc), "CURRENT_VERSION_RETIRE_REQUIRES_REPLACEMENT")
            if exc.code == CURRENT_VERSION_MISMATCH_CODE:
                return self._error(str(exc), "CURRENT_VERSION_MISMATCH")
            return self._error(str(exc), "INTERNAL_ERROR")
        if isinstance(exc, LookupError):
            return self._error(str(exc), "NOT_FOUND")
        if isinstance(exc, (ValueError, ValidationError)):
            return self._error(str(exc), "INVALID_PARAMS")
        return self._error(str(exc), "INTERNAL_ERROR")

    async def _call_boundary(self, context: Mapping[str, Any] | None, method_name: str, **kwargs: Any) -> CommandResult | ErrorResult:
        boundary = self._boundary(context)
        if boundary is None or not hasattr(boundary, method_name):
            return self._error("chunk text version boundary is unavailable", "VERSION_BOUNDARY_UNAVAILABLE")
        try:
            result = getattr(boundary, method_name)(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return CommandResult(data=dict(result))
        except Exception as exc:
            return self._map_exception(exc)


class ChunkVersionListCommand(_ChunkVersionCommand):
    """List version summaries for one semantic chunk."""

    name = "chunk_version_list"
    descr = "List semantic chunk text versions with lifecycle, current-state, and checksum metadata."
    detailed_description = (
        "Validates a UUID4 chunk identifier and pagination, then returns stable version summaries. "
        "The full text body is not returned by this command; use chunk_version_get when full text is required."
    )
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "include_deleted": {"type": "boolean", "default": False, "description": "Include soft-deleted lifecycle rows."},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_LIMIT,
            "default": 100,
            "description": "Maximum versions returned in this page.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "maximum": _MAX_OFFSET,
            "default": 0,
            "description": "Zero-based offset into the version history.",
        },
    }
    required_fields = ("chunk_id",)
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "include_deleted": {"type": "boolean", "description": "Include soft-deleted lifecycle rows.", "required": False},
        "limit": {"type": "integer", "description": "Maximum versions returned (1-1000).", "required": False},
        "offset": {"type": "integer", "description": "Zero-based version offset (0-10000000).", "required": False},
    }
    return_contract = {"description": "Chunk id, total version count, and version summary items."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000"}, {"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "limit": 20, "offset": 20}]

    async def execute(
        self,
        chunk_id: Any,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_include_deleted = self._parse_bool(include_deleted, "include_deleted")
            parsed_limit = self._parse_limit(limit)
            parsed_offset = self._parse_offset(offset)
        except Exception as exc:
            return self._map_exception(exc)
        response = await self._call_boundary(context, "list_versions", chunk_id=parsed_id, include_deleted=parsed_include_deleted)
        if not getattr(response, "success", False):
            return response
        payload = dict(response.data)
        items = [_summary(item) for item in payload.get("items", [])]
        payload["items"] = items[parsed_offset : parsed_offset + parsed_limit]
        payload.setdefault("total", len(items))
        payload["offset"] = parsed_offset
        payload["limit"] = parsed_limit
        return CommandResult(data=payload)


class ChunkHistoryCommand(ChunkVersionListCommand):
    """List the chunk version history using the explicit history command name."""

    name = "chunk_history"
    descr = "List semantic chunk text version history with lifecycle metadata."
    detailed_description = "Alias-style history command over the version boundary; it keeps the same pagination and summary rules as chunk_version_list."
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "include_deleted": True}]

    async def execute(
        self,
        chunk_id: Any,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_include_deleted = self._parse_bool(include_deleted, "include_deleted")
            parsed_limit = self._parse_limit(limit)
            parsed_offset = self._parse_offset(offset)
        except Exception as exc:
            return self._map_exception(exc)
        response = await self._call_boundary(context, "history", chunk_id=parsed_id, include_deleted=parsed_include_deleted)
        if not getattr(response, "success", False):
            return response
        payload = dict(response.data)
        items = [_summary(item) for item in payload.get("items", [])]
        payload["items"] = items[parsed_offset : parsed_offset + parsed_limit]
        payload.setdefault("total", len(items))
        payload["offset"] = parsed_offset
        payload["limit"] = parsed_limit
        return CommandResult(data=payload)


class ChunkVersionGetCommand(_ChunkVersionCommand):
    """Return one semantic chunk text version, optionally with full text."""

    name = "chunk_version_get"
    descr = "Get one semantic chunk text version by version number or current pointer."
    detailed_description = "When version_no is omitted or current=true, returns the active version. include_text controls whether the text body is included."
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "version_no": {"type": "integer", "minimum": 1, "description": "Optional positive version number."},
        "current": {"type": "boolean", "default": False, "description": "Resolve the current version pointer."},
        "include_text": {"type": "boolean", "default": True, "description": "Return the full version text."},
    }
    required_fields = ("chunk_id",)
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "version_no": {"type": "integer", "description": "Positive version number; omitted means current.", "required": False},
        "current": {"type": "boolean", "description": "Resolve active current version.", "required": False},
        "include_text": {"type": "boolean", "description": "Include full text body.", "required": False},
    }
    return_contract = {"description": "Chunk id and one version payload; full text is included when include_text is true."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "current": True}, {"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "version_no": 2, "include_text": False}]

    async def execute(
        self,
        chunk_id: Any,
        version_no: Any = None,
        current: bool = False,
        include_text: bool = True,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_version = self._parse_optional_version(version_no, "version_no")
            parsed_current = self._parse_bool(current, "current")
            parsed_include_text = self._parse_bool(include_text, "include_text")
        except Exception as exc:
            return self._map_exception(exc)
        return await self._call_boundary(
            context,
            "get_version",
            chunk_id=parsed_id,
            version_no=parsed_version,
            current=parsed_current,
            include_text=parsed_include_text,
        )


class _ChunkVersionTextMutationCommand(_ChunkVersionCommand):
    """Shared text mutation command implementation."""

    boundary_method: ClassVar[str]

    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "text": {"type": "string", "description": "New text body for the semantic chunk version."},
        "comment": {"type": "string", "description": "Optional human-readable change note."},
        "actor": {"type": "string", "description": "Optional actor identity for audit fields."},
        "expected_current_version": {"type": "integer", "minimum": 1, "description": "Optional optimistic-lock current version."},
        "operation_id": {"type": "string", "description": "Optional UUID4 operation id for batch/audit correlation."},
    }
    required_fields = ("chunk_id", "text")
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "text": {"type": "string", "description": "Text body for the new version.", "required": True},
        "comment": {"type": "string", "description": "Optional version comment.", "required": False},
        "actor": {"type": "string", "description": "Optional audit actor.", "required": False},
        "expected_current_version": {"type": "integer", "description": "Optional optimistic lock.", "required": False},
        "operation_id": {"type": "string", "description": "Optional UUID4 operation id.", "required": False},
    }
    return_contract = {"description": "Chunk id, outcome, and newly active version payload."}

    async def execute(
        self,
        chunk_id: Any,
        text: Any,
        comment: str | None = None,
        actor: str | None = None,
        expected_current_version: Any = None,
        operation_id: Any = None,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_text = self._parse_text(text)
            parsed_expected = self._parse_optional_version(expected_current_version, "expected_current_version")
            parsed_operation_id = self._parse_operation_id(operation_id)
        except Exception as exc:
            return self._map_exception(exc)
        return await self._call_boundary(
            context,
            self.boundary_method,
            chunk_id=parsed_id,
            text_value=parsed_text,
            comment=comment,
            actor=actor,
            expected_current_version=parsed_expected,
            operation_id=parsed_operation_id,
        )


class ChunkVersionAddCommand(_ChunkVersionTextMutationCommand):
    """Append a new active text version."""

    name = "chunk_version_add"
    boundary_method = "append_version"
    descr = "Append a new active semantic chunk text version."
    detailed_description = "Creates a new version number, retires the previous current version, updates semantic_chunk_current and semantic_chunk_texts, and invalidates derived data."
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "text": "New chunk text", "comment": "append correction", "expected_current_version": 1}]


class ChunkVersionUpdateCommand(_ChunkVersionTextMutationCommand):
    """Update a chunk by appending a replacement text version."""

    name = "chunk_version_update"
    boundary_method = "update_text"
    descr = "Update semantic chunk text non-destructively by appending a new active version."
    detailed_description = "This command is the public versioned update path: it never overwrites historical text. It appends a version and makes it current."
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "text": "Corrected text", "actor": "operator"}]


class ChunkVersionSetCurrentCommand(_ChunkVersionCommand):
    """Make an existing semantic chunk text version current."""

    name = "chunk_version_set_current"
    descr = "Set the current semantic chunk text version."
    detailed_description = "Promotes an existing non-deleted version to current, retires the previous current version, rewrites semantic_chunk_texts, and invalidates derived rows."
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "version_no": {"type": "integer", "minimum": 1, "description": "Positive version number."},
        "comment": {"type": "string", "description": "Optional promotion note."},
        "actor": {"type": "string", "description": "Optional audit actor."},
    }
    required_fields = ("chunk_id", "version_no")
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "version_no": {"type": "integer", "description": "Positive version number.", "required": True},
        "comment": {"type": "string", "description": "Optional promotion note.", "required": False},
        "actor": {"type": "string", "description": "Optional audit actor.", "required": False},
    }
    return_contract = {"description": "Chunk id, set-current outcome, and selected version payload."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "version_no": 2, "comment": "rollback"}]

    async def execute(
        self,
        chunk_id: Any,
        version_no: Any,
        comment: str | None = None,
        actor: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_version = self._parse_version(version_no)
        except Exception as exc:
            return self._map_exception(exc)
        return await self._call_boundary(
            context,
            "set_current",
            chunk_id=parsed_id,
            version_no=parsed_version,
            comment=comment,
            actor=actor,
        )


class ChunkVersionRestoreCommand(_ChunkVersionCommand):
    """Copy an older version into a new active version."""

    name = "chunk_version_restore"
    descr = "Restore an older semantic chunk text version by creating a new current version."
    detailed_description = "Does not move history backward. It copies the selected version's text into a fresh version, records restored_from_version_id, and makes the fresh version active."
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "version_no": {"type": "integer", "minimum": 1, "description": "Version to restore from."},
        "comment": {"type": "string", "description": "Optional restore note."},
        "actor": {"type": "string", "description": "Optional audit actor."},
        "expected_current_version": {"type": "integer", "minimum": 1, "description": "Optional optimistic-lock current version."},
        "operation_id": {"type": "string", "description": "Optional UUID4 operation id."},
    }
    required_fields = ("chunk_id", "version_no")
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "version_no": {"type": "integer", "description": "Version number to copy from.", "required": True},
        "comment": {"type": "string", "description": "Optional restore note.", "required": False},
        "actor": {"type": "string", "description": "Optional audit actor.", "required": False},
        "expected_current_version": {"type": "integer", "description": "Optional optimistic lock.", "required": False},
        "operation_id": {"type": "string", "description": "Optional UUID4 operation id.", "required": False},
    }
    return_contract = {"description": "Chunk id, restored outcome, and fresh current version payload."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "version_no": 1, "comment": "restore baseline"}]

    async def execute(
        self,
        chunk_id: Any,
        version_no: Any,
        comment: str | None = None,
        actor: str | None = None,
        expected_current_version: Any = None,
        operation_id: Any = None,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_version = self._parse_version(version_no)
            parsed_expected = self._parse_optional_version(expected_current_version, "expected_current_version")
            parsed_operation_id = self._parse_operation_id(operation_id)
        except Exception as exc:
            return self._map_exception(exc)
        return await self._call_boundary(
            context,
            "restore_version",
            chunk_id=parsed_id,
            version_no=parsed_version,
            comment=comment,
            actor=actor,
            expected_current_version=parsed_expected,
            operation_id=parsed_operation_id,
        )


class ChunkVersionRetireCommand(_ChunkVersionCommand):
    """Soft-retire one semantic chunk text version."""

    name = "chunk_version_retire"
    descr = "Retire a semantic chunk text version without physically deleting the row."
    detailed_description = "Sets lifecycle status to retired. If the retired version is current, replacement_version_no is required and becomes current first."
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "version_no": {"type": "integer", "minimum": 1, "description": "Version to retire."},
        "replacement_version_no": {"type": "integer", "minimum": 1, "description": "Required when retiring the current version."},
        "comment": {"type": "string", "description": "Optional retirement note."},
        "actor": {"type": "string", "description": "Optional audit actor."},
    }
    required_fields = ("chunk_id", "version_no")
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "version_no": {"type": "integer", "description": "Version to retire.", "required": True},
        "replacement_version_no": {"type": "integer", "description": "Replacement current version when retiring current.", "required": False},
        "comment": {"type": "string", "description": "Optional retirement note.", "required": False},
        "actor": {"type": "string", "description": "Optional audit actor.", "required": False},
    }
    return_contract = {"description": "Chunk id, retired version number, and current version number."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "version_no": 1}]

    async def execute(
        self,
        chunk_id: Any,
        version_no: Any,
        replacement_version_no: Any = None,
        comment: str | None = None,
        actor: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_version = self._parse_version(version_no)
            parsed_replacement = self._parse_optional_version(replacement_version_no, "replacement_version_no")
        except Exception as exc:
            return self._map_exception(exc)
        return await self._call_boundary(
            context,
            "retire_version",
            chunk_id=parsed_id,
            version_no=parsed_version,
            replacement_version_no=parsed_replacement,
            comment=comment,
            actor=actor,
        )


class ChunkVersionDeleteCommand(_ChunkVersionCommand):
    """Physically delete one semantic chunk text version."""

    name = "chunk_version_delete"
    descr = "Hard-delete a semantic chunk text version while preserving the last-version guard."
    detailed_description = "Administrative hard delete. Prefer chunk_version_retire for normal lifecycle removal. If the deleted row is current, the highest remaining version becomes current."
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "version_no": {"type": "integer", "minimum": 1, "description": "Positive version number."},
    }
    required_fields = ("chunk_id", "version_no")
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "version_no": {"type": "integer", "description": "Positive version number.", "required": True},
    }
    return_contract = {"description": "Chunk id, deleted version number, and resulting current version number."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "version_no": 1}]

    async def execute(self, chunk_id: Any, version_no: Any, context: Mapping[str, Any] | None = None) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_version = self._parse_version(version_no)
        except Exception as exc:
            return self._map_exception(exc)
        return await self._call_boundary(context, "delete_version", chunk_id=parsed_id, version_no=parsed_version)


class ChunkVersionDiffCommand(_ChunkVersionCommand):
    """Return a unified text diff between two chunk versions."""

    name = "chunk_version_diff"
    descr = "Diff two semantic chunk text versions."
    detailed_description = "Returns a unified line diff plus previews and checksums for both selected versions."
    schema_properties = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier."},
        "from_version_no": {"type": "integer", "minimum": 1, "description": "Left version number."},
        "to_version_no": {"type": "integer", "minimum": 1, "description": "Right version number."},
        "context_lines": {"type": "integer", "minimum": 0, "maximum": 20, "default": 3, "description": "Unified diff context lines."},
    }
    required_fields = ("chunk_id", "from_version_no", "to_version_no")
    parameter_docs = {
        "chunk_id": {"type": "string", "description": "Semantic chunk UUID4 identifier.", "required": True},
        "from_version_no": {"type": "integer", "description": "Left version number.", "required": True},
        "to_version_no": {"type": "integer", "description": "Right version number.", "required": True},
        "context_lines": {"type": "integer", "description": "Unified diff context lines, 0-20.", "required": False},
    }
    return_contract = {"description": "Chunk id, source/target version summaries, changed flag, and unified diff lines."}
    usage_examples = [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000", "from_version_no": 1, "to_version_no": 2, "context_lines": 2}]

    async def execute(
        self,
        chunk_id: Any,
        from_version_no: Any,
        to_version_no: Any,
        context_lines: int = 3,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            parsed_id = self._parse_chunk_id(chunk_id)
            parsed_from = self._parse_version(from_version_no, "from_version_no")
            parsed_to = self._parse_version(to_version_no, "to_version_no")
            parsed_context = self._parse_context_lines(context_lines)
        except Exception as exc:
            return self._map_exception(exc)
        return await self._call_boundary(
            context,
            "diff_versions",
            chunk_id=parsed_id,
            from_version_no=parsed_from,
            to_version_no=parsed_to,
            context_lines=parsed_context,
        )


def _summary(item: Mapping[str, Any]) -> dict[str, Any]:
    """Expose stable list fields without returning the stored text body."""

    digest = item.get("text_sha256") or item.get("checksum")
    text_value = item.get("text")
    preview = item.get("preview")
    if preview is None and isinstance(text_value, str):
        preview = text_value[:240]
    created_at = item.get("created_at")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    return {
        "id": item.get("id"),
        "logical_chunk_id": item.get("logical_chunk_id"),
        "version_no": item.get("version_no"),
        "preview": preview or "",
        "created_at": created_at,
        "current": bool(item.get("current", item.get("is_current", False))),
        "status": item.get("status", "active"),
        "valid_from": item.get("valid_from"),
        "valid_to": item.get("valid_to"),
        "char_count": item.get("char_count", len(text_value) if isinstance(text_value, str) else 0),
        "checksum": digest,
        "text_sha256": digest,
        "comment": item.get("comment"),
        "actor": item.get("actor"),
        "operation": item.get("operation"),
        "previous_version_id": item.get("previous_version_id"),
        "restored_from_version_id": item.get("restored_from_version_id"),
    }


__all__ = [
    "ChunkHistoryCommand",
    "ChunkVersionAddCommand",
    "ChunkVersionBoundary",
    "ChunkVersionDeleteCommand",
    "ChunkVersionDiffCommand",
    "ChunkVersionGetCommand",
    "ChunkVersionListCommand",
    "ChunkVersionRestoreCommand",
    "ChunkVersionRetireCommand",
    "ChunkVersionSetCurrentCommand",
    "ChunkVersionUpdateCommand",
]
