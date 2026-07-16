"""Shared command metadata base for doc-store public commands."""

from __future__ import annotations

from typing import Any, ClassVar

from mcp_proxy_adapter.commands.base import Command


class DocStoreCommand(Command):
    """Base metadata contract shared by doc-store commands."""

    version: ClassVar[str] = "0.1.0"
    descr: ClassVar[str] = ""
    category: ClassVar[str] = "doc-store"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False
    detailed_description: ClassVar[str] = (
        "Public doc-store command contract. Business behavior is implemented "
        "by later atomic command-handler steps."
    )
    schema_properties: ClassVar[dict[str, dict[str, str]]] = {}
    required_fields: ClassVar[tuple[str, ...]] = ()
    parameter_docs: ClassVar[dict[str, dict[str, str]]] = {}
    return_contract: ClassVar[dict[str, str]] = {
        "description": "Command-specific result contract."
    }
    usage_examples: ClassVar[list[dict[str, Any]]] = []
    best_practices: ClassVar[list[str]] = [
        "Use the adapter registry and generated help as the command authority."
    ]
    stable_errors: ClassVar[dict[str, str]] = {
        "NOT_IMPLEMENTED": "Command handler behavior is owned by a later atomic step."
    }

    async def execute(self, **kwargs: Any) -> Any:
        """Prevent accidental business execution before handler AS implement it."""

        raise NotImplementedError(f"{self.name} handler is not implemented")

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        """Return the complete public metadata contract for help generation."""

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
            "error_cases": cls.stable_errors,
            "best_practices": cls.best_practices,
        }

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """Return the public JSON schema contract for this command."""

        schema: dict[str, Any] = {
            "type": "object",
            "properties": dict(cls.schema_properties),
            "required": list(cls.required_fields),
            "additionalProperties": False,
        }
        if cls.use_queue:
            schema["x-use-queue"] = True
        return schema


_DocStoreCommand = DocStoreCommand


__all__ = ["DocStoreCommand", "_DocStoreCommand"]
