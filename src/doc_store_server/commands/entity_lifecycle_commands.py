"""Public CRUD/lifecycle commands for addressable doc-store entities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.commands.result import ErrorResult

from doc_store_server.runtime.entity_lifecycle import (
    DeletionSafetyError,
    EntityLifecycleService,
    installed_entity_lifecycle_service,
)


class EntityLifecycleBoundary(Protocol):
    def list_entities(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def get_entity(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def soft_delete(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def undelete(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def hard_delete(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def references_for(self, **kwargs: Any) -> Mapping[str, Any]: ...


ENTITY_TYPES = ("documents", "chapters", "paragraphs", "semantic_chunks", "projects")


class _EntityCommand(Command):
    version: ClassVar[str] = "0.1.0"
    category: ClassVar[str] = "doc-store.lifecycle"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False
    lifecycle_boundary: ClassVar[EntityLifecycleBoundary | None] = None
    _description: ClassVar[str]

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
                "Executes addressable entity CRUD/lifecycle operations through the "
                "installed runtime lifecycle boundary. Deleted rows are hidden by "
                "default from list/get commands unless show_deleted is true."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {"description": "Command-specific entity lifecycle result."},
            "usage_examples": cls.usage_examples(),
            "error_cases": {
                "INVALID_PARAMS": "Unknown entity type, malformed id, field, filter, limit, or offset.",
                "LIFECYCLE_BOUNDARY_UNAVAILABLE": "Database lifecycle boundary is not configured.",
                "DELETE_BLOCKED": "Hard delete would leave references outside the delete set.",
                "NOT_FOUND": "The requested entity is absent or hidden by the deletion filter.",
            },
            "best_practices": [
                "Use entity_hard_delete only after checking entity_references.",
                "Use show_deleted only for lifecycle administration screens.",
            ],
        }

    @classmethod
    def usage_examples(cls) -> list[dict[str, Any]]:
        return []

    def _boundary(self, context: Mapping[str, Any] | None = None) -> EntityLifecycleBoundary | None:
        if context and context.get("entity_lifecycle_boundary") is not None:
            return context["entity_lifecycle_boundary"]
        return self.lifecycle_boundary or installed_entity_lifecycle_service()

    @staticmethod
    def _error(message: str, *, code: str = "INVALID_PARAMS", details: Mapping[str, Any] | None = None) -> ErrorResult:
        return ErrorResult(message, details={"code": code, **dict(details or {})})


class EntityListCommand(_EntityCommand):
    name = "entity_list"
    descr = "List addressable entities with filters, field selection and pagination."
    _description = descr

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "enum": list(ENTITY_TYPES), "description": "Entity table/scope to list."},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "Optional returned fields."},
                "filters": {"type": "object", "description": "Exact-match filters, including block_meta.<key>."},
                "limit": {"type": "integer", "description": "Page size from 1 to 500."},
                "offset": {"type": "integer", "description": "Zero-based page offset."},
                "show_deleted": {"type": "boolean", "description": "Include rows marked deleted."},
            },
            "required": ["entity_type"],
            "additionalProperties": False,
        }

    @classmethod
    def usage_examples(cls) -> list[dict[str, Any]]:
        return [{"entity_type": "documents", "limit": 20}, {"entity_type": "semantic_chunks", "filters": {"block_meta.project": "doc-store"}}]

    async def execute(
        self,
        entity_type: str,
        fields: Sequence[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        limit: int = 50,
        offset: int = 0,
        show_deleted: bool = False,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        boundary = self._boundary(context)
        if boundary is None:
            return self._error("Entity lifecycle boundary is unavailable.", code="LIFECYCLE_BOUNDARY_UNAVAILABLE")
        try:
            return CommandResult(
                data=boundary.list_entities(
                    entity_type=entity_type,
                    fields=fields,
                    filters=filters,
                    limit=limit,
                    offset=offset,
                    show_deleted=show_deleted,
                )
            )
        except Exception as exc:
            return self._error(str(exc))


class EntityGetCommand(_EntityCommand):
    name = "entity_get"
    descr = "Get one addressable entity by UUID."
    _description = descr

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "enum": list(ENTITY_TYPES), "description": "Entity table/scope."},
                "entity_id": {"type": "string", "description": "UUID4 entity identifier."},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "Optional returned fields."},
                "show_deleted": {"type": "boolean", "description": "Return rows marked deleted."},
            },
            "required": ["entity_type", "entity_id"],
            "additionalProperties": False,
        }

    @classmethod
    def usage_examples(cls) -> list[dict[str, Any]]:
        return [{"entity_type": "paragraphs", "entity_id": "550e8400-e29b-41d4-a716-446655440000"}]

    async def execute(
        self,
        entity_type: str,
        entity_id: str,
        fields: Sequence[str] | None = None,
        show_deleted: bool = False,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        boundary = self._boundary(context)
        if boundary is None:
            return self._error("Entity lifecycle boundary is unavailable.", code="LIFECYCLE_BOUNDARY_UNAVAILABLE")
        try:
            return CommandResult(data=boundary.get_entity(entity_type=entity_type, entity_id=entity_id, fields=fields, show_deleted=show_deleted))
        except LookupError as exc:
            return self._error(str(exc), code="NOT_FOUND")
        except Exception as exc:
            return self._error(str(exc))


class _EntityIdsCommand(_EntityCommand):
    action: ClassVar[str]

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "enum": list(ENTITY_TYPES), "description": "Entity table/scope."},
                "ids": {"type": "array", "items": {"type": "string"}, "description": "UUID4 ids or project names for entity_type=projects."},
            },
            "required": ["entity_type", "ids"],
            "additionalProperties": False,
        }

    @classmethod
    def usage_examples(cls) -> list[dict[str, Any]]:
        return [{"entity_type": "documents", "ids": ["550e8400-e29b-41d4-a716-446655440000"]}]

    async def execute(
        self,
        entity_type: str,
        ids: Sequence[str],
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        boundary = self._boundary(context)
        if boundary is None:
            return self._error("Entity lifecycle boundary is unavailable.", code="LIFECYCLE_BOUNDARY_UNAVAILABLE")
        try:
            method = getattr(boundary, self.action)
            return CommandResult(data=method(entity_type=entity_type, ids=ids))
        except DeletionSafetyError as exc:
            return self._error(str(exc), code="DELETE_BLOCKED")
        except Exception as exc:
            return self._error(str(exc))


class EntitySoftDeleteCommand(_EntityIdsCommand):
    name = "entity_soft_delete"
    descr = "Soft delete addressable entities and dependent owned units."
    _description = descr
    action = "soft_delete"


class EntityUndeleteCommand(_EntityIdsCommand):
    name = "entity_undelete"
    descr = "Clear the soft-delete marker on addressable entities and dependent owned units."
    _description = descr
    action = "undelete"


class EntityHardDeleteCommand(_EntityIdsCommand):
    name = "entity_hard_delete"
    descr = "Permanently delete addressable entities after reference-safety validation."
    _description = descr
    action = "hard_delete"


class EntityReferencesCommand(_EntityCommand):
    name = "entity_references"
    descr = "Return records that reference one addressable entity."
    _description = descr

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "enum": list(ENTITY_TYPES), "description": "Entity table/scope."},
                "entity_id": {"type": "string", "description": "UUID4 entity identifier or project name."},
            },
            "required": ["entity_type", "entity_id"],
            "additionalProperties": False,
        }

    @classmethod
    def usage_examples(cls) -> list[dict[str, Any]]:
        return [{"entity_type": "semantic_chunks", "entity_id": "550e8400-e29b-41d4-a716-446655440000"}]

    async def execute(
        self,
        entity_type: str,
        entity_id: str,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        boundary = self._boundary(context)
        if boundary is None:
            return self._error("Entity lifecycle boundary is unavailable.", code="LIFECYCLE_BOUNDARY_UNAVAILABLE")
        try:
            return CommandResult(data=boundary.references_for(entity_type=entity_type, entity_id=entity_id))
        except Exception as exc:
            return self._error(str(exc))


__all__ = [
    "EntityGetCommand",
    "EntityHardDeleteCommand",
    "EntityLifecycleBoundary",
    "EntityListCommand",
    "EntityReferencesCommand",
    "EntitySoftDeleteCommand",
    "EntityUndeleteCommand",
]
