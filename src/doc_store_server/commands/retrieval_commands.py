"""Typed document-hierarchy retrieval commands.

The command layer owns parameter and result contracts only.  Retrieval is
provided by the application through the adapter command context.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, ClassVar, Protocol
from uuid import UUID

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.core.errors import ValidationError


class RetrievalBoundary(Protocol):
    """Canonical application retrieval/query boundary used by these commands."""

    async def get_document(self, document_id: UUID, source_version: int | None = None) -> Any:
        """Return one document result."""

    async def get_chapter(self, chapter_id: UUID, source_version: int | None = None) -> Any:
        """Return one chapter result."""

    async def get_paragraph(self, paragraph_id: UUID, source_version: int | None = None) -> Any:
        """Return one paragraph result."""


class InvalidVersionError(ValueError):
    """Raised by the retrieval boundary when a requested version is invalid."""


def _typed_identifier(value: Any, field: str, command_name: str) -> UUID:
    """Parse a UUID4 identifier so the boundary receives a typed value."""

    try:
        identifier = value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError(
            f"{command_name}: parameter {field!r} must be a UUID4 identifier",
            data={"field": field, "value": value},
        ) from exc
    if identifier.version != 4:
        raise ValidationError(
            f"{command_name}: parameter {field!r} must be a UUID4 identifier",
            data={"field": field, "value": value},
        )
    return identifier


def _typed_result(value: Any) -> Any:
    """Convert common typed result models without changing their public fields."""

    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return value


class _RetrievalCommand(Command):
    """Shared adapter contract and execution/error mapping for retrieval commands."""

    category: ClassVar[str] = "retrieval"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    version: ClassVar[str] = "0.1.0"
    use_queue: ClassVar[bool] = False
    identifier_field: ClassVar[str]
    entity_name: ClassVar[str]
    boundary_method: ClassVar[str]
    descr: ClassVar[str]
    detailed_description: ClassVar[str]
    schema_properties: ClassVar[dict[str, dict[str, Any]]]
    required_fields: ClassVar[tuple[str, ...]]
    parameter_docs: ClassVar[dict[str, dict[str, Any]]]
    return_contract: ClassVar[dict[str, Any]]
    usage_examples: ClassVar[list[dict[str, Any]]]
    best_practices: ClassVar[list[str]]
    retrieval_boundary: ClassVar[RetrievalBoundary | None] = None

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
                "NOT_FOUND": {
                    "description": f"The requested {cls.entity_name} is not visible.",
                    "message": f"{cls.entity_name.title()} not found.",
                    "solution": "Retry with an identifier returned by the canonical query boundary.",
                },
                "INVALID_VERSION": {
                    "description": "The requested source version is not valid for the entity.",
                    "message": "The requested source_version is invalid.",
                    "solution": "Omit source_version for the current version or use a visible positive version.",
                },
                "INTERNAL_ERROR": {
                    "description": "The canonical retrieval boundary failed unexpectedly.",
                    "message": "Retrieval failed internally.",
                    "solution": "Inspect the service diagnostics and retry the command.",
                },
            },
            "best_practices": cls.best_practices,
        }

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        validated = super().validate_params(params)
        validated[self.identifier_field] = _typed_identifier(
            validated[self.identifier_field], self.identifier_field, self.name
        )
        source_version = validated.get("source_version")
        if source_version is not None and (
            isinstance(source_version, bool)
            or not isinstance(source_version, int)
            or source_version <= 0
        ):
            raise ValidationError(
                f"{self.name}: source_version must be a positive integer",
                data={"field": "source_version", "value": source_version},
            )
        return validated

    async def execute(self, **kwargs: Any) -> CommandResult:
        context = kwargs.pop("context", {})
        boundary = context.get("retrieval_boundary") if isinstance(context, Mapping) else None
        if boundary is None:
            boundary = self.retrieval_boundary
        if boundary is None or not hasattr(boundary, self.boundary_method):
            return CommandResult(success=False, error="INTERNAL_ERROR: retrieval boundary unavailable")

        identifier = kwargs[self.identifier_field]
        source_version = kwargs.get("source_version")
        try:
            result = getattr(boundary, self.boundary_method)(identifier, source_version)
            if inspect.isawaitable(result):
                result = await result
        except InvalidVersionError as exc:
            return CommandResult(success=False, error=f"INVALID_VERSION: {exc}")
        except LookupError as exc:
            return CommandResult(success=False, error=f"NOT_FOUND: {exc}")
        except Exception as exc:
            return CommandResult(success=False, error=f"INTERNAL_ERROR: {exc}")

        return CommandResult(
            success=True,
            data={
                "entity": self.entity_name,
                "identifier": str(identifier),
                "source_version": source_version,
                "value": _typed_result(result),
            },
        )


class DocumentGetCommand(_RetrievalCommand):
    name = "document_get"
    identifier_field = "document_id"
    entity_name = "document"
    boundary_method = "get_document"
    descr = "Retrieve one typed document by UUID."
    detailed_description = "Validates a document UUID and optional positive source version, then delegates to the canonical retrieval boundary."
    schema_properties = {
        "document_id": {"type": "string", "description": "Document UUID4 identifier."},
        "source_version": {"type": "integer", "description": "Optional positive document source version."},
    }
    required_fields = ("document_id",)
    parameter_docs = {
        "document_id": {"type": "string", "description": "Document UUID4 identifier.", "required": True},
        "source_version": {"type": "integer", "description": "Optional positive document source version.", "required": False},
    }
    return_contract = {"description": "Stable typed document retrieval envelope.", "data": {"value": "Document data."}}
    usage_examples = [{"document_id": "550e8400-e29b-41d4-a716-446655440000"}]
    best_practices = ["Use identifiers returned by the canonical document query boundary."]

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return super().get_schema()

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return super().metadata()

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return super().validate_params(params)

    async def execute(self, **kwargs: Any) -> CommandResult:
        return await super().execute(**kwargs)


class ChapterGetCommand(_RetrievalCommand):
    name = "chapter_get"
    identifier_field = "chapter_id"
    entity_name = "chapter"
    boundary_method = "get_chapter"
    descr = "Retrieve one typed chapter by UUID."
    detailed_description = "Validates a chapter UUID and optional positive source version, then delegates to the canonical retrieval boundary."
    schema_properties = DocumentGetCommand.schema_properties | {"chapter_id": {"type": "string", "description": "Chapter UUID4 identifier."}}
    schema_properties.pop("document_id")
    required_fields = ("chapter_id",)
    parameter_docs = {"chapter_id": {"type": "string", "description": "Chapter UUID4 identifier.", "required": True}, "source_version": DocumentGetCommand.parameter_docs["source_version"]}
    return_contract = {"description": "Stable typed chapter retrieval envelope.", "data": {"value": "Chapter data."}}
    usage_examples = [{"chapter_id": "550e8400-e29b-41d4-a716-446655440000"}]
    best_practices = ["Use chapter identifiers returned by the canonical document hierarchy."]

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return super().get_schema()

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return super().metadata()

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return super().validate_params(params)

    async def execute(self, **kwargs: Any) -> CommandResult:
        return await super().execute(**kwargs)


class ParagraphGetCommand(_RetrievalCommand):
    name = "paragraph_get"
    identifier_field = "paragraph_id"
    entity_name = "paragraph"
    boundary_method = "get_paragraph"
    descr = "Retrieve one typed paragraph by UUID."
    detailed_description = "Validates a paragraph UUID and optional positive source version, then delegates to the canonical retrieval boundary."
    schema_properties = DocumentGetCommand.schema_properties | {"paragraph_id": {"type": "string", "description": "Paragraph UUID4 identifier."}}
    schema_properties.pop("document_id")
    required_fields = ("paragraph_id",)
    parameter_docs = {"paragraph_id": {"type": "string", "description": "Paragraph UUID4 identifier.", "required": True}, "source_version": DocumentGetCommand.parameter_docs["source_version"]}
    return_contract = {"description": "Stable typed paragraph retrieval envelope.", "data": {"value": "Paragraph data."}}
    usage_examples = [{"paragraph_id": "550e8400-e29b-41d4-a716-446655440000"}]
    best_practices = ["Use paragraph identifiers returned by the canonical chapter hierarchy."]

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return super().get_schema()

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return super().metadata()

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return super().validate_params(params)

    async def execute(self, **kwargs: Any) -> CommandResult:
        return await super().execute(**kwargs)


__all__ = [
    "ChapterGetCommand",
    "DocumentGetCommand",
    "InvalidVersionError",
    "ParagraphGetCommand",
    "RetrievalBoundary",
]
