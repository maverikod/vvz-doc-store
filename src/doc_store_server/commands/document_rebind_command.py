"""Adapter command for rebinding document project and chunk metadata."""

from __future__ import annotations

import inspect
import os
from collections.abc import Awaitable, Mapping
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.core.errors import ValidationError

from doc_store_server.commands.validation import parse_uuid4
from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.document_rebind import (
    DocumentRebindError,
    DocumentRebindService,
)


class DocumentRebindBoundary(Protocol):
    """Boundary for metadata-only document rebinding."""

    def rebind_document(
        self,
        *,
        document_id: str,
        project: str | None = None,
        project_id: str | None = None,
        project_description: str | None = None,
        document_properties: Mapping[str, Any] | None = None,
        chunk_properties: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Rebind one document and its addressable child metadata."""


class DocumentRebindCommand(Command):
    """Rebind an existing document to a project and chunk properties."""

    name = "document_rebind"
    version = "0.1.0"
    descr = "Rebind an existing document to a project and chunk metadata."
    category = "doc-store"
    author = "Vasiliy Zdanovskiy"
    email = "vasilyvz@gmail.com"
    use_queue = False

    rebind_boundary: ClassVar[DocumentRebindBoundary | None] = None

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Existing document UUID to rebind.",
                },
                "project": {
                    "type": "string",
                    "description": "Optional project value copied to document and chunk metadata.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Required project UUID when project is supplied.",
                },
                "project_description": {
                    "type": "string",
                    "description": "Required project comment/short description when project is supplied.",
                },
                "document_properties": {
                    "type": "object",
                    "description": "Optional metadata keys merged into the document block_meta.",
                },
                "chunk_properties": {
                    "type": "object",
                    "description": (
                        "Optional metadata keys merged into chapter, paragraph, and semantic "
                        "chunk block_meta."
                    ),
                },
            },
            "required": ["document_id"],
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
            "detailed_description": (
                "Updates only addressable-unit metadata for an existing document. "
                "The command preserves document text, unit ordering, chunks, and "
                "embedding rows while consistently merging project and chunk "
                "properties into block_meta."
            ),
            "parameters": {
                "document_id": "Required existing document UUID.",
                "project": "Optional project value applied to document and chunk metadata.",
                "project_id": "Required UUID4 project identifier when project is supplied.",
                "project_description": "Required project comment or short description when project is supplied.",
                "document_properties": "Optional object merged into documents.block_meta.",
                "chunk_properties": (
                    "Optional object merged into chapters, paragraphs, and "
                    "semantic_chunks block_meta."
                ),
            },
            "return_value": {
                "description": "Rebind outcome, requested document, applied values, and row counts."
            },
            "usage_examples": [
                {
                    "document_id": "550e8400-e29b-41d4-a716-446655440000",
                    "project": "doc-store",
                    "project_id": "7254b86c-7456-47b3-8b7d-1590eef0f4a5",
                    "project_description": "Runtime docs project.",
                    "chunk_properties": {"scope": "runtime", "tags": ["client"]},
                }
            ],
            "error_cases": {
                "INVALID_PARAMS": "Malformed document_id, project_id, blank project/description, or non-object metadata.",
                "NO_REBIND_FIELDS": "At least one rebind field must be supplied.",
                "DATABASE_NOT_CONFIGURED": "The installed runtime has no database URL.",
                "DOCUMENT_NOT_FOUND": "The requested document does not exist.",
            },
            "best_practices": [
                "Use this command for reassignment without re-uploading or re-vectorizing.",
                "Use document_update or document_chunk when text or chunking strategy must change.",
            ],
        }

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        validated = super().validate_params(params)
        document_id = validated.get("document_id")
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValidationError("document_id must be a non-empty UUID string", {"field": "document_id"})
        validated["document_id"] = str(parse_uuid4(document_id, "document_id", self.name))

        project = validated.get("project")
        if project is not None:
            if not isinstance(project, str) or not project.strip():
                raise ValidationError("project must be a non-empty string", {"field": "project"})
            validated["project"] = project.strip()
            project_id = validated.get("project_id")
            if not isinstance(project_id, str) or not project_id.strip():
                raise ValidationError("project_id is required when project is supplied", {"field": "project_id"})
            validated["project_id"] = str(parse_uuid4(project_id, "project_id", self.name))
            project_description = validated.get("project_description")
            if not isinstance(project_description, str) or not project_description.strip():
                raise ValidationError(
                    "project_description is required when project is supplied",
                    {"field": "project_description"},
                )
            validated["project_description"] = project_description.strip()
        else:
            for field in ("project_id", "project_description"):
                if validated.get(field) is not None:
                    raise ValidationError(f"{field} requires project", {"field": field})

        for field in ("document_properties", "chunk_properties"):
            value = validated.get(field)
            if value is not None and not isinstance(value, Mapping):
                raise ValidationError(f"{field} must be an object", {"field": field})
            if value is not None:
                validated[field] = dict(value)

        if not any(
            validated.get(field) is not None
            for field in ("project", "document_properties", "chunk_properties")
        ):
            raise ValidationError(
                "at least one rebind field is required",
                {"code": "NO_REBIND_FIELDS"},
            )
        return validated

    async def execute(
        self,
        *,
        context: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> CommandResult:
        try:
            params = self.validate_params(kwargs)
        except ValidationError as exc:
            return CommandResult(success=False, error=str(exc), data={"details": exc.data})

        boundary = self._resolve_boundary(context)
        try:
            result = boundary.rebind_document(**params)
            if inspect.isawaitable(result):
                result = await result
        except DocumentRebindError as exc:
            return CommandResult(
                success=False,
                error=str(exc),
                data={
                    "outcome": exc.code.lower(),
                    "document_id": params["document_id"],
                    "details": exc.details,
                },
            )
        except Exception as exc:
            return CommandResult(
                success=False,
                error=str(exc),
                data={"outcome": "failed", "document_id": params["document_id"]},
            )
        return CommandResult(success=True, data=dict(result))

    @classmethod
    def _resolve_boundary(cls, context: Mapping[str, Any] | None) -> DocumentRebindBoundary:
        if context:
            boundary = context.get("document_rebind_boundary")
            if boundary is not None:
                return boundary
        if cls.rebind_boundary is not None:
            return cls.rebind_boundary
        database_url = database_url_from_config(dict(context or {}))
        if not database_url:
            database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
        return DocumentRebindService(database_url)


__all__ = ["DocumentRebindBoundary", "DocumentRebindCommand"]
