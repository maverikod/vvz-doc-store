"""Read-only projection of ingestion runtime status."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.core.errors import ValidationError


class IngestionRuntimeStatusBoundary(Protocol):
    """The existing ingestion-owned status lookup consumed by this command."""

    def get_status(
        self, operation_id: str, document_id: str | None = None
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Return the current status without changing runtime state."""


StatusLookup = Callable[
    [str, str | None], Mapping[str, Any] | Awaitable[Mapping[str, Any]]
]


class ProcessingStatusCommand(Command):
    """Expose ingestion status while keeping orchestration outside the command."""

    name = "processing_status"
    version = "0.1.0"
    descr = "Return the current read-only status of one ingestion operation."
    category = "doc-store"
    author = "Vasiliy Zdanovskiy"
    email = "vasilyvz@gmail.com"
    use_queue = False

    status_vocabulary: ClassVar[tuple[str, ...]] = (
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
    )
    runtime_status_boundary: ClassVar[
        IngestionRuntimeStatusBoundary | StatusLookup | None
    ] = None

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """Return a strict schema for operation and optional document correlation."""

        return {
            "type": "object",
            "properties": {
                "operation_id": {
                    "type": "string",
                    "description": "Required ingestion operation identifier.",
                },
                "document_id": {
                    "type": "string",
                    "description": "Optional canonical document correlation.",
                },
            },
            "required": ["operation_id"],
            "additionalProperties": False,
        }

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        """Return complete adapter help metadata for the command."""

        parameters = cls.get_schema()["properties"]
        return {
            "name": cls.name,
            "version": cls.version,
            "description": cls.descr,
            "category": cls.category,
            "author": cls.author,
            "email": cls.email,
            "detailed_description": (
                "Projects ingestion-owned runtime status for one operation. "
                "This command is read-only and does not start, retry, cancel, "
                "poll, persist, or transport work."
            ),
            "parameters": parameters,
            "return_value": {
                "description": (
                    "Stable status, progress, timestamps, canonical references, "
                    "and structured failure diagnostics when available."
                )
            },
            "usage_examples": [
                {"operation_id": "00000000-0000-4000-8000-000000000000"},
                {
                    "operation_id": "00000000-0000-4000-8000-000000000000",
                    "document_id": "00000000-0000-4000-8000-000000000001",
                },
            ],
            "error_cases": {
                "INVALID_PARAMS": "Missing, empty, unknown, or inconsistent correlation.",
                "STATUS_LOOKUP_FAILED": "The ingestion status boundary failed.",
                "INVALID_STATUS": "The boundary returned an unsupported state.",
            },
            "best_practices": [
                "Use operation and document identifiers returned by ingestion commands.",
                "Treat the result as a snapshot and keep orchestration in ingestion.",
            ],
        }

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply adapter validation and reject blank identifiers explicitly."""

        validated = super().validate_params(params)
        for field in ("operation_id", "document_id"):
            value = validated.get(field)
            if value is not None and not value.strip():
                raise ValidationError(
                    f"processing_status: parameter {field!r} must not be empty",
                    data={"field": field},
                )
        return validated

    async def execute(
        self,
        operation_id: str,
        document_id: str | None = None,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        """Look up and normalize one status snapshot through ingestion."""

        boundary = self._resolve_boundary(context)
        if boundary is None:
            return self._failure(
                operation_id,
                "STATUS_LOOKUP_FAILED",
                "Ingestion runtime-status boundary is unavailable.",
            )

        try:
            raw = (
                boundary(operation_id, document_id)
                if callable(boundary)
                else boundary.get_status(operation_id, document_id)
            )
            if inspect.isawaitable(raw):
                raw = await raw
            if not isinstance(raw, Mapping):
                raise TypeError("status boundary must return a mapping")
            data = self._project(raw, operation_id, document_id)
        except ValueError as exc:
            message = str(exc)
            code = (
                "INVALID_PARAMS"
                if "does not match requested document_id" in message
                else "INVALID_STATUS"
                if "unsupported status" in message
                else "STATUS_LOOKUP_FAILED"
            )
            return self._failure(
                operation_id,
                code,
                message,
                document_id=document_id,
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            return self._failure(
                operation_id,
                "STATUS_LOOKUP_FAILED",
                str(exc),
                document_id=document_id,
                error_type=type(exc).__name__,
            )
        return CommandResult(success=True, data=data)

    @classmethod
    def _resolve_boundary(
        cls, context: Mapping[str, Any] | None
    ) -> IngestionRuntimeStatusBoundary | StatusLookup | None:
        if context:
            for key in ("ingestion_runtime_status", "runtime_status_boundary"):
                candidate = context.get(key)
                if candidate is not None:
                    return candidate
        return cls.runtime_status_boundary

    @classmethod
    def _project(
        cls, raw: Mapping[str, Any], operation_id: str, document_id: str | None
    ) -> dict[str, Any]:
        status = raw.get("status")
        if status not in cls.status_vocabulary:
            raise ValueError(f"unsupported status {status!r}")

        returned_document_id = raw.get("document_id")
        if (
            document_id is not None
            and returned_document_id is not None
            and str(returned_document_id) != document_id
        ):
            raise ValueError("status document_id does not match requested document_id")

        data: dict[str, Any] = {
            "operation_id": operation_id,
            "status": status,
            "progress": raw.get("progress"),
            "timestamps": raw.get("timestamps", {}),
            "document_reference": raw.get("document_reference"),
            "version_reference": raw.get("version_reference"),
            "failure": raw.get("failure"),
        }
        if returned_document_id is not None:
            data["document_id"] = returned_document_id
        if document_id is not None:
            data["requested_document_id"] = document_id
        return data

    @staticmethod
    def _failure(
        operation_id: str,
        code: str,
        message: str,
        *,
        document_id: str | None = None,
        error_type: str | None = None,
    ) -> CommandResult:
        diagnostic: dict[str, Any] = {"code": code, "message": message}
        if error_type:
            diagnostic["type"] = error_type
        data: dict[str, Any] = {
            "operation_id": operation_id,
            "status": "failed",
            "progress": None,
            "timestamps": {},
            "document_reference": None,
            "version_reference": None,
            "failure": diagnostic,
        }
        if document_id is not None:
            data["requested_document_id"] = document_id
        return CommandResult(success=False, data=data, error=message)


__all__ = ["IngestionRuntimeStatusBoundary", "ProcessingStatusCommand"]
