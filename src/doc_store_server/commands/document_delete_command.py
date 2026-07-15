"""Adapter command for deleting one canonical document version."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.core.errors import ValidationError


class CanonicalDocumentService(Protocol):
    """Boundary for the canonical, atomic document deletion operation."""

    def delete_document(
        self, document_id: str, version_token: str
    ) -> Mapping[str, Any] | str | Awaitable[Mapping[str, Any] | str]:
        """Delete a document under the supplied version precondition."""


class DocumentDeleteCommand(Command):
    """Delete one document through the canonical document service boundary."""

    name = "document_delete"
    version = "0.1.0"
    descr = "Delete one document when its version precondition matches."
    category = "doc-store"
    author = "Vasiliy Zdanovskiy"
    email = "vasilyvz@gmail.com"
    use_queue = False

    document_service: ClassVar[CanonicalDocumentService | None] = None
    outcomes: ClassVar[tuple[str, ...]] = (
        "deleted",
        "already_absent",
        "conflict",
    )

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """Return the strict deletion request schema."""

        return {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Canonical document identifier to delete.",
                },
                "version_token": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Required version or deletion precondition token; the "
                        "service must reject a stale token."
                    ),
                },
            },
            "required": ["document_id", "version_token"],
            "additionalProperties": False,
        }

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        """Return complete adapter help metadata for this command."""

        return {
            "name": cls.name,
            "version": cls.version,
            "description": cls.descr,
            "category": cls.category,
            "author": cls.author,
            "email": cls.email,
            "detailed_description": (
                "Deletes a canonical document only when the supplied version "
                "token is still current. The canonical document service owns "
                "the atomic operation and its document hierarchy; this command "
                "does not expose intermediate state."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {
                "description": (
                    "Stable outcome with one of deleted, already_absent, or "
                    "conflict, plus the requested document identifier."
                ),
                "schema": {
                    "type": "object",
                    "properties": {
                        "outcome": {
                            "type": "string",
                            "enum": list(cls.outcomes),
                        },
                        "document_id": {"type": "string"},
                    },
                    "required": ["outcome", "document_id"],
                    "additionalProperties": False,
                },
            },
            "usage_examples": [
                {
                    "document_id": "00000000-0000-4000-8000-000000000000",
                    "version_token": "document-version-7",
                },
                {
                    "document_id": "doc-123",
                    "version_token": "etag:4f2c9a",
                },
            ],
            "error_cases": {
                "INVALID_PARAMS": (
                    "Provide non-empty document_id and version_token only; "
                    "remove unknown fields."
                ),
                "CONFLICT": (
                    "The version token is stale or the canonical service could "
                    "not establish a deletion precondition; refresh the document "
                    "and retry with its current token."
                ),
                "SERVICE_UNAVAILABLE": (
                    "The canonical document service is unavailable; retry after "
                    "the service is restored."
                ),
            },
            "best_practices": [
                "Always supply the version token read with the document.",
                "Treat already_absent as an idempotent successful result.",
                "Treat conflict as requiring a fresh read; do not retry the same token.",
                "Rely on the canonical document service for atomicity and visibility.",
            ],
        }

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Validate required identifiers and reject blank string values."""

        validated = super().validate_params(params)
        for field in ("document_id", "version_token"):
            value = validated[field]
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"{self.name}: parameter {field!r} must be a non-empty string",
                    data={"field": field},
                )
            validated[field] = value.strip()
        return validated

    async def execute(
        self,
        document_id: str,
        version_token: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        """Delegate one deletion and project only its stable terminal outcome."""

        service = self._resolve_service(context)
        if service is None:
            return self._failure(
                document_id,
                "SERVICE_UNAVAILABLE",
                "Canonical document service is unavailable.",
            )

        try:
            result = service.delete_document(document_id, version_token)
            if inspect.isawaitable(result):
                result = await result
            outcome = self._outcome(result)
        except Exception:
            return self._failure(
                document_id,
                "CONFLICT",
                "Document deletion could not establish its required precondition.",
            )

        if outcome not in self.outcomes:
            return self._failure(
                document_id,
                "CONFLICT",
                "Canonical document service returned an invalid deletion outcome.",
            )
        data = {"outcome": outcome, "document_id": document_id}
        return CommandResult(success=outcome != "conflict", data=data)

    @classmethod
    def _resolve_service(
        cls, context: Mapping[str, Any] | None
    ) -> CanonicalDocumentService | None:
        if context:
            for key in ("canonical_document_service", "document_service"):
                service = context.get(key)
                if service is not None:
                    return service
        return cls.document_service

    @classmethod
    def _outcome(cls, result: Mapping[str, Any] | str) -> str:
        if isinstance(result, str):
            return result
        if isinstance(result, Mapping):
            outcome = result.get("outcome", result.get("status"))
            return outcome if isinstance(outcome, str) else ""
        return ""

    @staticmethod
    def _failure(document_id: str, code: str, message: str) -> CommandResult:
        return CommandResult(
            success=False,
            data={
                "outcome": "conflict" if code == "CONFLICT" else code.lower(),
                "document_id": document_id,
            },
            error=message,
        )


__all__ = ["CanonicalDocumentService", "DocumentDeleteCommand"]
