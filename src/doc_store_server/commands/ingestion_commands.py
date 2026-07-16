"""Public document-ingestion commands.

The commands validate the adapter-facing request and hand it to the G-006
ingestion boundary.  They intentionally do not own normalization, queues,
storage, or any downstream processing stage.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar
from uuid import UUID, NAMESPACE_URL, uuid5

from mcp_proxy_adapter.commands.base import Command
from mcp_proxy_adapter.commands.result import ErrorResult, SuccessResult
from mcp_proxy_adapter.core.errors import ValidationError


Boundary = Callable[
    ..., Awaitable[Mapping[str, Any] | None] | Mapping[str, Any] | None
]
_BOUNDARY_CONTEXT_KEY = "ingestion_boundary"
_STATES = frozenset({"accepted", "idempotent", "completed", "failed"})
_CHUNKING_STRATEGIES = frozenset({"paragraph", "sentence", "semantic"})
_KNOWN_FIELDS = frozenset(
    {"document_id", "source_version_id", "raw_text", "transferred_file", "chunking_strategy"}
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _operation_id(document_id: str, source_version_id: str) -> str:
    """Derive a replay-stable operation identity from the source identity."""

    return str(
        uuid5(NAMESPACE_URL, f"doc-store:operation:{document_id}:{source_version_id}")
    )


def _validate_chunking_strategy(
    params: dict[str, Any],
    *,
    required: bool,
) -> str | None:
    strategy = params.get("chunking_strategy")
    if strategy is None:
        if required:
            raise ValidationError(
                "chunking_strategy is required",
                {"field": "chunking_strategy", "allowed": sorted(_CHUNKING_STRATEGIES)},
            )
        return None
    if not isinstance(strategy, str) or strategy.strip() not in _CHUNKING_STRATEGIES:
        raise ValidationError(
            "chunking_strategy must be one of paragraph, sentence, semantic",
            {"field": "chunking_strategy", "allowed": sorted(_CHUNKING_STRATEGIES)},
        )
    return strategy.strip()


def _validate_identity(params: dict[str, Any], *, chunking_required: bool) -> dict[str, Any]:
    document_id = params.get("document_id")
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValidationError(
            "document_id must be a non-empty UUID string", {"field": "document_id"}
        )
    try:
        document_uuid = UUID(document_id)
    except (ValueError, AttributeError) as exc:
        raise ValidationError(
            "document_id must be a valid UUID", {"field": "document_id"}
        ) from exc

    source_version_id = params.get("source_version_id")
    if not isinstance(source_version_id, str) or not source_version_id.strip():
        raise ValidationError(
            "source_version_id must be a non-empty string",
            {"field": "source_version_id"},
        )

    has_raw_text = "raw_text" in params
    has_transferred_file = "transferred_file" in params
    if has_raw_text == has_transferred_file:
        raise ValidationError(
            "exactly one of raw_text or transferred_file is required",
            {"code": "INVALID_SOURCE_COUNT"},
        )
    if has_raw_text and (not isinstance(params["raw_text"], str) or not params["raw_text"]):
        raise ValidationError("raw_text must be a non-empty string", {"field": "raw_text"})
    if has_transferred_file and not isinstance(
        params["transferred_file"], Mapping
    ) and not callable(getattr(params["transferred_file"], "read", None)):
        raise ValidationError(
            "transferred_file must be an adapter transfer reference",
            {"field": "transferred_file"},
        )

    normalized = dict(params)
    normalized["document_id"] = str(document_uuid)
    normalized["source_version_id"] = source_version_id.strip()
    strategy = _validate_chunking_strategy(normalized, required=chunking_required)
    if strategy is not None:
        normalized["chunking_strategy"] = strategy
    return normalized


def _result_payload(
    *,
    status: str,
    document_id: str,
    source_version_id: str,
    operation_id: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status if status in _STATES else "failed",
        "operation_id": operation_id,
        "document_id": document_id,
        "source_version_id": source_version_id,
    }
    if details:
        payload.update(details)
    return payload


class _IngestionCommand(Command):
    """Shared strict adapter contract for create and update ingestion."""

    version: ClassVar[str] = "0.1.0"
    category: ClassVar[str] = "doc-store.ingestion"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = _env_bool("DOC_STORE_INGESTION_USE_QUEUE", True)
    result_class: ClassVar[type[SuccessResult]] = SuccessResult
    _description: ClassVar[str]
    chunking_strategy_required: ClassVar[bool] = False
    ingestion_boundary: ClassVar[Boundary | None] = None

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        required = ["document_id", "source_version_id"]
        if cls.chunking_strategy_required:
            required.append("chunking_strategy")
        return {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Document UUID."},
                "source_version_id": {
                    "type": "string",
                    "description": "Stable source-version identity.",
                },
                "raw_text": {"type": "string", "description": "Raw UTF-8 source text."},
                "transferred_file": {
                    "type": "object",
                    "description": "Adapter-standard transferred file reference.",
                },
                "chunking_strategy": {
                    "type": "string",
                    "enum": ["paragraph", "sentence", "semantic"],
                    "description": "Document chunking strategy.",
                },
            },
            "required": required,
            "additionalProperties": False,
            "x-oneOf": ["raw_text", "transferred_file"],
            "x-use-queue": cls.use_queue,
        }

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "version": cls.version,
            "description": cls._description,
            "category": cls.category,
            "author": cls.author,
            "email": cls.email,
            "detailed_description": (
                "Validates the public adapter command request, enforces exactly "
                "one source, and delegates all ingestion behavior to the G-006 "
                "orchestration boundary without implementing transfer, queue, "
                "WebSocket, chunking, embedding, persistence, registration, or "
                "REST behavior."
            ),
            "parameters": {
                "document_id": "Required UUID document identity.",
                "source_version_id": "Required stable source-version identity.",
                "raw_text": "Exactly one accepted source: raw text.",
                "transferred_file": "Exactly one accepted source: adapter transfer reference.",
                "chunking_strategy": (
                    "Required for document_create; optional for update/rechunk where the stored "
                    "document strategy is reused when omitted."
                ),
            },
            "return_value": {
                "description": (
                    "Accepted, idempotent, completed, or failed ingestion state "
                    "with stable identities."
                )
            },
            "error_cases": {
                "INVALID_SOURCE_COUNT": "Exactly one source field must be present.",
                "INVALID_PARAMS": "Unknown fields or malformed identities are rejected.",
                "INGESTION_BOUNDARY_UNAVAILABLE": "The G-006 boundary was not supplied.",
            },
            "usage_examples": [
                {
                    "document_id": "550e8400-e29b-41d4-a716-446655440000",
                    "source_version_id": "source-v1",
                    "chunking_strategy": "paragraph",
                    "raw_text": "Example documentation text.",
                },
                {
                    "document_id": "550e8400-e29b-41d4-a716-446655440000",
                    "source_version_id": "source-v2",
                    "chunking_strategy": "semantic",
                    "transferred_file": {"transfer_id": "adapter-transfer-id"},
                },
            ],
            "best_practices": ["Pass large files through the adapter transfer primitive."],
        }

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValidationError("command parameters must be an object")
        unknown = sorted(set(params) - _KNOWN_FIELDS)
        if unknown:
            raise ValidationError("unknown command fields", {"fields": unknown})
        return _validate_identity(
            dict(super().validate_params(params)),
            chunking_required=self.chunking_strategy_required,
        )

    async def execute(self, context: Any = None, **kwargs: Any) -> SuccessResult | ErrorResult:
        try:
            params = self.validate_params(kwargs)
        except ValidationError as exc:
            return ErrorResult(str(exc), code=exc.code, details=exc.data)

        document_id = params["document_id"]
        source_version_id = params["source_version_id"]
        operation_id = _operation_id(document_id, source_version_id)
        boundary = context.get(_BOUNDARY_CONTEXT_KEY) if isinstance(context, Mapping) else None
        if boundary is None:
            boundary = self.ingestion_boundary
        if boundary is None:
            from doc_store_server.ingestion.runtime_boundary import installed_ingestion_boundary

            boundary = installed_ingestion_boundary()
        if boundary is None:
            return ErrorResult(
                "G-006 ingestion boundary is unavailable",
                code=-32603,
                details=_result_payload(
                    status="failed",
                    document_id=document_id,
                    source_version_id=source_version_id,
                    operation_id=operation_id,
                    details={"error": "INGESTION_BOUNDARY_UNAVAILABLE"},
                ),
            )

        try:
            outcome = boundary(**params, operation_id=operation_id, command=self.name)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            outcome_map = dict(outcome or {})
            raw_status = str(outcome_map.pop("status", "accepted"))
            status = {
                "committed": "completed",
                "idempotent_replay": "idempotent",
                "rolled_back": "failed",
            }.get(raw_status, raw_status)
            return SuccessResult(
                _result_payload(
                    status=status,
                    document_id=document_id,
                    source_version_id=source_version_id,
                    operation_id=operation_id,
                    details=outcome_map,
                )
            )
        except Exception as exc:
            return SuccessResult(
                _result_payload(
                    status="failed",
                    document_id=document_id,
                    source_version_id=source_version_id,
                    operation_id=operation_id,
                    details={"error": type(exc).__name__, "message": str(exc)},
                )
            )


class DocumentCreateCommand(_IngestionCommand):
    """Create one document source version through G-006."""

    name = "document_create"
    descr = "Create a document version from exactly one adapter-owned source."
    _description = descr
    chunking_strategy_required = True


class DocumentUpdateCommand(_IngestionCommand):
    """Update one document source version through G-006."""

    name = "document_update"
    descr = "Update a document with exactly one adapter-owned source."
    _description = descr


class DocumentChunkCommand(_IngestionCommand):
    """Rechunk an existing document, optionally overriding its stored strategy."""

    name = "document_chunk"
    descr = "Rechunk an existing document using its stored chunking strategy unless overridden."
    _description = descr

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        schema = super().get_schema()
        schema["properties"] = {
            "document_id": schema["properties"]["document_id"],
            "chunking_strategy": schema["properties"]["chunking_strategy"],
        }
        schema["required"] = ["document_id"]
        schema.pop("x-oneOf", None)
        return schema

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        metadata = super().metadata()
        metadata["parameters"] = {
            "document_id": "Required UUID document identity.",
            "chunking_strategy": (
                "Optional strategy override; when omitted, the stored document strategy is reused."
            ),
        }
        metadata["usage_examples"] = [
            {"document_id": "550e8400-e29b-41d4-a716-446655440000"},
            {
                "document_id": "550e8400-e29b-41d4-a716-446655440000",
                "chunking_strategy": "sentence",
            },
        ]
        metadata["error_cases"] = {
            "CHUNKING_STRATEGY_REQUIRED": "The existing document has no stored strategy and none was supplied.",
            "DOCUMENT_NOT_FOUND": "The document does not exist.",
            "INGESTION_BOUNDARY_UNAVAILABLE": "The G-006 boundary was not supplied.",
        }
        return metadata

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValidationError("command parameters must be an object")
        unknown = sorted(set(params) - {"document_id", "chunking_strategy"})
        if unknown:
            raise ValidationError("unknown command fields", {"fields": unknown})
        document_id = params.get("document_id")
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValidationError(
                "document_id must be a non-empty UUID string", {"field": "document_id"}
            )
        try:
            document_uuid = UUID(document_id)
        except (ValueError, AttributeError) as exc:
            raise ValidationError(
                "document_id must be a valid UUID", {"field": "document_id"}
            ) from exc
        normalized = dict(params)
        normalized["document_id"] = str(document_uuid)
        strategy = _validate_chunking_strategy(normalized, required=False)
        if strategy is not None:
            normalized["chunking_strategy"] = strategy
        return normalized

    async def execute(self, context: Any = None, **kwargs: Any) -> SuccessResult | ErrorResult:
        try:
            params = self.validate_params(kwargs)
        except ValidationError as exc:
            return ErrorResult(str(exc), code=exc.code, details=exc.data)

        document_id = params["document_id"]
        params.setdefault("source_version_id", "stored")
        source_version_id = params.get("source_version_id", "stored")
        operation_id = _operation_id(document_id, f"chunk:{source_version_id}:{params.get('chunking_strategy', 'stored')}")
        boundary = context.get(_BOUNDARY_CONTEXT_KEY) if isinstance(context, Mapping) else None
        if boundary is None:
            boundary = self.ingestion_boundary
        if boundary is None:
            from doc_store_server.ingestion.runtime_boundary import installed_ingestion_boundary

            boundary = installed_ingestion_boundary()
        if boundary is None:
            return ErrorResult(
                "G-006 ingestion boundary is unavailable",
                code=-32603,
                details=_result_payload(
                    status="failed",
                    document_id=document_id,
                    source_version_id=source_version_id,
                    operation_id=operation_id,
                    details={"error": "INGESTION_BOUNDARY_UNAVAILABLE"},
                ),
            )

        try:
            outcome = boundary(**params, operation_id=operation_id, command=self.name)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            outcome_map = dict(outcome or {})
            raw_status = str(outcome_map.pop("status", "accepted"))
            status = {
                "committed": "completed",
                "idempotent_replay": "idempotent",
                "rolled_back": "failed",
            }.get(raw_status, raw_status)
            return SuccessResult(
                _result_payload(
                    status=status,
                    document_id=document_id,
                    source_version_id=str(outcome_map.get("source_version_id", source_version_id)),
                    operation_id=operation_id,
                    details=outcome_map,
                )
            )
        except Exception as exc:
            return SuccessResult(
                _result_payload(
                    status="failed",
                    document_id=document_id,
                    source_version_id=source_version_id,
                    operation_id=operation_id,
                    details={"error": type(exc).__name__, "message": str(exc)},
                )
            )


__all__ = ("DocumentChunkCommand", "DocumentCreateCommand", "DocumentUpdateCommand")
