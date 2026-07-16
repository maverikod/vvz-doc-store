"""UUID4 generation command."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.commands.result import ErrorResult


class Uuid4Command(Command):
    """Generate one UUID4 or a deterministic-size batch of UUID4 strings."""

    name = "uuid4"
    version: ClassVar[str] = "0.1.0"
    descr: ClassVar[str] = "Generate one UUID4 value or a list of UUID4 values."
    category: ClassVar[str] = "doc-store.utility"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Number of UUID4 values to generate. Defaults to 1.",
                },
            },
            "required": [],
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
                "Returns UUID version 4 identifiers for use in commands that require "
                "strict UUID4 fields. With no parameters it returns one value; with "
                "count it returns a list of that size."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {
                "description": "For count=1 returns uuid4; for count>1 returns uuid4_list."
            },
            "usage_examples": [{}, {"count": 5}],
            "error_cases": {
                "INVALID_PARAMS": "count must be an integer between 1 and 1000.",
            },
            "best_practices": [
                "Use this command to prepare identifiers before calling UUID4-strict commands."
            ],
        }

    async def execute(self, count: int = 1, context: Any = None) -> CommandResult | ErrorResult:
        del context
        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 1000:
            return ErrorResult(
                "count must be an integer between 1 and 1000",
                details={"code": "INVALID_PARAMS", "field": "count"},
            )
        values = [str(uuid4()) for _ in range(count)]
        data: dict[str, Any] = {"count": count}
        if count == 1:
            data["uuid4"] = values[0]
        else:
            data["uuid4_list"] = values
        return CommandResult(data=data)


__all__ = ["Uuid4Command"]
