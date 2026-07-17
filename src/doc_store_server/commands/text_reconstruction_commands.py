"""Commands for reconstructing text from current ordered chunks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.commands.result import ErrorResult

from doc_store_server.commands.validation import parse_optional_uuid4
from doc_store_server.runtime.text_reconstruction import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_CHARS,
    METADATA_FILTER_KEY_RE,
    installed_text_reconstruction_service,
)


class TextReconstructionBoundary(Protocol):
    def assemble_chapter_text(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def reconstruct_source_file(self, **kwargs: Any) -> Mapping[str, Any]: ...


class _TextReconstructionCommand(Command):
    """Shared validation and error mapping for text reconstruction commands."""

    version: ClassVar[str] = "0.1.0"
    category: ClassVar[str] = "doc-store.reconstruction"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False
    reconstruction_boundary: ClassVar[TextReconstructionBoundary | None] = None

    @classmethod
    def _common_properties(cls) -> dict[str, Any]:
        return {
            "document_id": {"type": "string", "description": "Optional document UUID4 selector."},
            "file_id": {"type": "string", "description": "Optional file/source UUID4 selector."},
            "source_name": {"type": "string", "description": "Optional exact source filename selector."},
            "source_path": {"type": "string", "description": "Optional exact source path selector."},
            "project_id": {"type": "string", "description": "Optional project UUID4 selector."},
            "metadata_filters": {
                "type": "object",
                "description": "Optional exact string filters over semantic chunk block_meta.",
                "additionalProperties": True,
            },
            "max_chars": {
                "type": "integer",
                "minimum": 0,
                "maximum": 5_000_000,
                "default": DEFAULT_MAX_CHARS,
                "description": "Maximum characters returned inline. 0 disables truncation.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100_000,
                "default": DEFAULT_LIMIT,
                "description": "Maximum chunks to scan for this page.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "maximum": 10_000_000,
                "default": 0,
                "description": "Zero-based chunk offset for very large reconstructions.",
            },
        }

    @classmethod
    def _common_metadata(cls) -> dict[str, Any]:
        return {
            "version": cls.version,
            "category": cls.category,
            "author": cls.author,
            "email": cls.email,
            "error_cases": {
                "INVALID_PARAMS": "Malformed UUID, empty selector, invalid limit/offset/max_chars, or invalid metadata filters.",
                "NOT_FOUND": "No visible current chunk text matched the selector.",
                "RECONSTRUCTION_BOUNDARY_UNAVAILABLE": "Database reconstruction boundary is not configured.",
                "INTERNAL_ERROR": "Unexpected reconstruction failure.",
            },
            "best_practices": [
                "Use UUID selectors when available; source_name/source_path are exact-match fallbacks.",
                "Use max_chars plus limit/offset for large sources and then request further pages.",
                "range_map maps returned text ranges back to chunk_id, paragraph_id, source offsets, and previews.",
            ],
        }

    def _boundary(self, context: Mapping[str, Any] | None) -> TextReconstructionBoundary | None:
        boundary = context.get("text_reconstruction_boundary") if isinstance(context, Mapping) else None
        if boundary is None:
            boundary = self.reconstruction_boundary or installed_text_reconstruction_service()
        return boundary


class ChapterTextGetCommand(_TextReconstructionCommand):
    """Assemble chapter text from ordered current semantic chunks."""

    name = "chapter_text_get"
    descr = "Assemble chapter-level text from current ordered chunks."

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        properties = {
            "chapter_id": {"type": "string", "description": "Optional chapter UUID4 selector."},
            **cls._common_properties(),
            "include_context": {
                "type": "boolean",
                "default": False,
                "description": "Reserved flag for neighbor context; current response records the requested mode.",
            },
        }
        return {"type": "object", "properties": properties, "required": [], "additionalProperties": False}

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        common = cls._common_metadata()
        return {
            "name": cls.name,
            "description": cls.descr,
            "detailed_description": (
                "Reconstructs chapter text from semantic_chunk_texts joined through the canonical "
                "Document -> Chapter -> Paragraph -> SemanticChunk hierarchy. It returns inline UTF-8 "
                "text, checksum, source identifiers, and range_map entries that map returned text ranges "
                "back to current chunk ids and paragraph ids. It is corpus-agnostic and never depends on "
                "7d numbering or domain-specific chapter labels."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {"description": "Chapter text, checksum, source ids, range map, and pagination guards."},
            "usage_examples": [
                {"chapter_id": "550e8400-e29b-41d4-a716-446655440000"},
                {"document_id": "550e8400-e29b-41d4-a716-446655440001", "metadata_filters": {"chapter_code": "intro"}},
            ],
            **common,
        }

    async def execute(
        self,
        chapter_id: str | None = None,
        document_id: str | None = None,
        file_id: str | None = None,
        source_name: str | None = None,
        source_path: str | None = None,
        project_id: str | None = None,
        metadata_filters: Mapping[str, Any] | None = None,
        include_context: bool = False,
        max_chars: int = DEFAULT_MAX_CHARS,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            params = _validated_common(
                chapter_id=chapter_id,
                document_id=document_id,
                file_id=file_id,
                source_name=source_name,
                source_path=source_path,
                project_id=project_id,
                metadata_filters=metadata_filters,
                max_chars=max_chars,
                limit=limit,
                offset=offset,
                command_name=self.name,
            )
        except Exception as exc:
            return ErrorResult(str(exc), details={"code": "INVALID_PARAMS"})
        if not _has_selector(params):
            return ErrorResult(
                "chapter_id or at least one selector is required",
                details={"code": "INVALID_PARAMS"},
            )
        boundary = self._boundary(context)
        if boundary is None:
            return ErrorResult(
                "Text reconstruction boundary is unavailable.",
                details={"code": "RECONSTRUCTION_BOUNDARY_UNAVAILABLE"},
            )
        try:
            return CommandResult(data=boundary.assemble_chapter_text(include_context=bool(include_context), **params))
        except LookupError as exc:
            return ErrorResult(str(exc), details={"code": "NOT_FOUND"})
        except ValueError as exc:
            return ErrorResult(str(exc), details={"code": "INVALID_PARAMS"})
        except Exception as exc:
            return ErrorResult(str(exc), details={"code": "INTERNAL_ERROR"})


class SourceFileReconstructCommand(_TextReconstructionCommand):
    """Reconstruct source file text from ordered current semantic chunks."""

    name = "source_file_reconstruct"
    descr = "Reconstruct source-file text from current ordered chunks."

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {"type": "object", "properties": cls._common_properties(), "required": [], "additionalProperties": False}

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        common = cls._common_metadata()
        return {
            "name": cls.name,
            "description": cls.descr,
            "detailed_description": (
                "Reconstructs the text representation of a stored source file from current active "
                "semantic_chunk_texts. Select by file_id, document_id, source_name, source_path, project_id, "
                "or metadata_filters. The command preserves source order and returns range_map provenance, "
                "checksums, truncation status, and source/document/chapter identifiers. Office/PDF binary "
                "retrieval remains a future transfer/download concern; this command reconstructs the text "
                "payload stored after ingestion."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {"description": "Source text, checksum, source ids, range map, and pagination guards."},
            "usage_examples": [
                {"file_id": "550e8400-e29b-41d4-a716-446655440000", "max_chars": 100000},
                {"source_name": "chapter.md", "limit": 5000, "offset": 0},
            ],
            **common,
        }

    async def execute(
        self,
        document_id: str | None = None,
        file_id: str | None = None,
        source_name: str | None = None,
        source_path: str | None = None,
        project_id: str | None = None,
        metadata_filters: Mapping[str, Any] | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            params = _validated_common(
                chapter_id=None,
                document_id=document_id,
                file_id=file_id,
                source_name=source_name,
                source_path=source_path,
                project_id=project_id,
                metadata_filters=metadata_filters,
                max_chars=max_chars,
                limit=limit,
                offset=offset,
                command_name=self.name,
            )
            params.pop("chapter_id", None)
        except Exception as exc:
            return ErrorResult(str(exc), details={"code": "INVALID_PARAMS"})
        if not _has_selector(params):
            return ErrorResult(
                "at least one source selector is required",
                details={"code": "INVALID_PARAMS"},
            )
        boundary = self._boundary(context)
        if boundary is None:
            return ErrorResult(
                "Text reconstruction boundary is unavailable.",
                details={"code": "RECONSTRUCTION_BOUNDARY_UNAVAILABLE"},
            )
        try:
            return CommandResult(data=boundary.reconstruct_source_file(**params))
        except LookupError as exc:
            return ErrorResult(str(exc), details={"code": "NOT_FOUND"})
        except ValueError as exc:
            return ErrorResult(str(exc), details={"code": "INVALID_PARAMS"})
        except Exception as exc:
            return ErrorResult(str(exc), details={"code": "INTERNAL_ERROR"})


def _validated_common(
    *,
    chapter_id: str | None,
    document_id: str | None,
    file_id: str | None,
    source_name: str | None,
    source_path: str | None,
    project_id: str | None,
    metadata_filters: Mapping[str, Any] | None,
    max_chars: int,
    limit: int,
    offset: int,
    command_name: str,
) -> dict[str, Any]:
    filters = dict(metadata_filters or {})
    for key in filters:
        if not isinstance(key, str) or not METADATA_FILTER_KEY_RE.fullmatch(key):
            raise ValueError(
                "metadata_filters keys must match ^[A-Za-z_][A-Za-z0-9_]{0,127}$"
            )
    return {
        "chapter_id": _optional_uuid(chapter_id, "chapter_id", command_name),
        "document_id": _optional_uuid(document_id, "document_id", command_name),
        "file_id": _optional_uuid(file_id, "file_id", command_name),
        "source_name": _optional_text(source_name, "source_name"),
        "source_path": _optional_text(source_path, "source_path"),
        "project_id": _optional_uuid(project_id, "project_id", command_name),
        "metadata_filters": filters or None,
        "max_chars": _bounded_int(max_chars, "max_chars", 0, 5_000_000),
        "limit": _bounded_int(limit, "limit", 1, 100_000),
        "offset": _bounded_int(offset, "offset", 0, 10_000_000),
    }


def _optional_uuid(value: str | None, field: str, command_name: str) -> str | None:
    parsed = parse_optional_uuid4(value, field, command_name)
    return str(parsed) if parsed is not None else None


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _bounded_int(value: int, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def _has_selector(params: Mapping[str, Any]) -> bool:
    return any(
        params.get(key)
        for key in (
            "chapter_id",
            "document_id",
            "file_id",
            "source_name",
            "source_path",
            "project_id",
            "metadata_filters",
        )
    )


__all__ = [
    "ChapterTextGetCommand",
    "SourceFileReconstructCommand",
    "TextReconstructionBoundary",
]
