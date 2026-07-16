"""Public command for safe SemanticChunk metadata updates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.commands.result import ErrorResult

from doc_store_server.commands.validation import parse_uuid4
from doc_store_server.runtime.semantic_chunk_metadata import (
    SemanticChunkMetadataService,
    installed_semantic_chunk_metadata_service,
)


class SemanticChunkMetadataBoundary(Protocol):
    def update_metadata(self, **kwargs: Any) -> Mapping[str, Any]: ...


SAFE_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Safe SemanticChunk metadata patch. Text, embeddings, evaluator quality "
        "scores, tokens, and BM25 tokens are intentionally forbidden."
    ),
    "properties": {
        "type": {"type": "string", "description": "SemanticChunk type dictionary value."},
        "role": {"type": "string", "description": "SemanticChunk role dictionary value."},
        "status": {"type": "string", "description": "SemanticChunk status dictionary value."},
        "block_type": {"type": "string", "description": "Block type dictionary value."},
        "language": {"type": "string", "description": "Language dictionary value."},
        "category": {"type": "string", "description": "Category dictionary value."},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Complete ordered replacement tag list for this chunk.",
        },
        "summary": {
            "type": ["string", "null"],
            "description": "Machine or human summary stored in block_meta only.",
        },
        "title": {
            "type": ["string", "null"],
            "description": "Machine or human title stored in block_meta only.",
        },
        "classification": {
            "type": "object",
            "description": (
                "Optional provenance for weak/local model classifications. Store "
                "provider, model, model_version, prompt_version, confidence, evidence, "
                "and review_status so later evaluator workers can audit or replace it."
            ),
            "properties": {
                "provider": {"type": "string"},
                "model": {"type": "string"},
                "model_version": {"type": "string"},
                "prompt_version": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {"type": "string"},
                "review_status": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}


class SemanticChunkMetadataUpdateCommand(Command):
    """Update safe metadata fields on one or more semantic chunks."""

    name = "semantic_chunk_metadata_update"
    version = "0.1.0"
    descr = "Update safe SemanticChunk classifier metadata and provenance."
    category = "doc-store.semantic-chunk"
    author = "Vasiliy Zdanovskiy"
    email = "vasilyvz@gmail.com"
    use_queue = False
    metadata_boundary: ClassVar[SemanticChunkMetadataBoundary | None] = None

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "version": cls.version,
            "description": cls.descr,
            "category": cls.category,
            "author": cls.author,
            "email": cls.email,
            "detailed_description": (
                "Specialized SemanticChunk metadata writer. Use this instead of "
                "generic entity_update for chunk classifier metadata. It updates "
                "dictionary compatibility columns, normalized classifier assignment "
                "child rows, block_meta, and tag rows in one transaction. It is designed "
                "for future weak/local open-source classifiers: each patch may include "
                "classification provenance and confidence. It deliberately rejects "
                "text, embedding, token/BM25 token, and evaluator quality fields."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {
                "description": (
                    "Outcome, requested/matched/updated counts, and per-chunk previews."
                )
            },
            "usage_examples": [
                {
                    "chunk_id": "550e8400-e29b-41d4-a716-446655440000",
                    "updates": {
                        "language": "ru",
                        "category": "theory",
                        "classification": {
                            "provider": "local",
                            "model": "small-open-source-classifier",
                            "confidence": 0.74,
                            "review_status": "machine",
                        },
                    },
                    "dry_run": True,
                },
                {
                    "items": [
                        {
                            "chunk_id": "550e8400-e29b-41d4-a716-446655440000",
                            "updates": {"category": "theory", "language": "ru"},
                        }
                    ]
                },
                {
                    "filters": {"seven_d_min": 50, "seven_d_max": 99},
                    "updates": {"status": "needs_review"},
                    "limit": 500,
                },
            ],
            "error_cases": {
                "INVALID_PARAMS": (
                    "Malformed UUID, selector, filter, metadata patch, or forbidden field."
                ),
                "SEMANTIC_CHUNK_METADATA_BOUNDARY_UNAVAILABLE": (
                    "Database metadata update boundary is not configured."
                ),
                "NOT_FOUND": "A selected chunk id does not exist.",
            },
            "best_practices": [
                "Use dry_run before large selector updates.",
                "For weak model outputs, include classification.confidence and evidence.",
                "Do not use this command for evaluator quality fields; those belong to a separate worker.",
                "Batch model-specific per-chunk values through items rather than direct database writes.",
            ],
        }

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        item_schema = {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string", "description": "SemanticChunk UUID."},
                "updates": SAFE_UPDATE_SCHEMA,
            },
            "required": ["chunk_id", "updates"],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string", "description": "Single SemanticChunk UUID selector."},
                "chunk_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Multiple SemanticChunk UUID selector.",
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Selector filters: document_id, paragraph_id, chapter_id, file_id, "
                        "project_id, source_name, seven_d_number, seven_d_min, seven_d_max."
                    ),
                },
                "updates": SAFE_UPDATE_SCHEMA,
                "items": {
                    "type": "array",
                    "items": item_schema,
                    "description": "Per-chunk metadata patches for classifier output batches.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10000,
                    "description": "Maximum selected chunks for filter-based updates.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10000000,
                    "description": "Offset for filter-based updates.",
                },
                "include_deleted": {
                    "type": "boolean",
                    "description": "Include soft-deleted chunks when selecting by filters.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview selected updates without writing to the database.",
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    def _boundary(
        self,
        context: Mapping[str, Any] | None,
    ) -> SemanticChunkMetadataBoundary | None:
        if context and context.get("semantic_chunk_metadata_boundary") is not None:
            return context["semantic_chunk_metadata_boundary"]
        return self.metadata_boundary or installed_semantic_chunk_metadata_service()

    async def execute(
        self,
        chunk_id: str | None = None,
        chunk_ids: Sequence[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        updates: Mapping[str, Any] | None = None,
        items: Sequence[Mapping[str, Any]] | None = None,
        limit: int = 100,
        offset: int = 0,
        include_deleted: bool = False,
        dry_run: bool = False,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        boundary = self._boundary(context)
        if boundary is None:
            return _error(
                "SemanticChunk metadata boundary is unavailable.",
                code="SEMANTIC_CHUNK_METADATA_BOUNDARY_UNAVAILABLE",
            )
        try:
            parsed_chunk_id = (
                str(parse_uuid4(chunk_id, "chunk_id", self.name))
                if chunk_id is not None
                else None
            )
            parsed_chunk_ids = (
                [
                    str(parse_uuid4(item, f"chunk_ids[{index}]", self.name))
                    for index, item in enumerate(chunk_ids)
                ]
                if chunk_ids is not None
                else None
            )
            parsed_filters = _validated_filters(filters, self.name) if filters is not None else None
            parsed_items = _validated_items(items, self.name) if items is not None else None
            return CommandResult(
                data=boundary.update_metadata(
                    updates=updates,
                    items=parsed_items,
                    chunk_id=parsed_chunk_id,
                    chunk_ids=parsed_chunk_ids,
                    filters=parsed_filters,
                    limit=limit,
                    offset=offset,
                    include_deleted=include_deleted,
                    dry_run=dry_run,
                )
            )
        except LookupError as exc:
            return _error(str(exc), code="NOT_FOUND")
        except Exception as exc:
            return _error(str(exc))


def _validated_items(
    items: Sequence[Mapping[str, Any]] | None,
    command_name: str,
) -> list[dict[str, Any]]:
    if items is None:
        return []
    result = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"items[{index}] must be an object")
        chunk_id = str(parse_uuid4(item.get("chunk_id"), f"items[{index}].chunk_id", command_name))
        updates = item.get("updates")
        if not isinstance(updates, Mapping):
            raise ValueError(f"items[{index}].updates must be an object")
        result.append({"chunk_id": chunk_id, "updates": dict(updates)})
    return result


def _validated_filters(
    filters: Mapping[str, Any] | None,
    command_name: str,
) -> dict[str, Any] | None:
    if filters is None:
        return None
    if not isinstance(filters, Mapping):
        raise ValueError("filters must be an object")
    result = dict(filters)
    for key in ("document_id", "paragraph_id", "chapter_id", "file_id", "project_id"):
        if key in result:
            result[key] = str(parse_uuid4(result[key], f"filters.{key}", command_name))
    return result


def _error(message: str, *, code: str = "INVALID_PARAMS") -> ErrorResult:
    return ErrorResult(message, details={"code": code})


__all__ = ["SAFE_UPDATE_SCHEMA", "SemanticChunkMetadataUpdateCommand"]
