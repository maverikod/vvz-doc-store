"""Adapter command for the canonical ChunkQuery search contract."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, ClassVar, Protocol

from chunk_metadata_adapter import ChunkQuery, ChunkQueryResponse, SearchResult
from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.core.errors import ValidationError as AdapterValidationError
from mcp_proxy_adapter.commands.result import ErrorResult
from pydantic import ValidationError as PydanticValidationError


class SearchOrchestrator(Protocol):
    """G-007 execution boundary owned by the application search layer."""

    def __call__(self, query: ChunkQuery, **context: Any) -> Any:
        """Execute one normalized query."""


def _query_schema() -> dict[str, Any]:
    schema = ChunkQuery.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def _serialize_result(result: Any) -> dict[str, Any]:
    """Serialize the already-ranked G-007 response without changing it."""

    if isinstance(result, ChunkQueryResponse):
        return result.to_dict()
    if isinstance(result, Mapping):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        serialized = to_dict()
        if isinstance(serialized, Mapping):
            return dict(serialized)
    if isinstance(result, (list, tuple)) and all(
        isinstance(item, SearchResult) for item in result
    ):
        return {"status": "success", "data": {"results": [item.to_dict() for item in result]}}
    raise TypeError("G-007 search orchestrator returned an unsupported response")


class ChunkQuerySearchCommand(Command):
    """Validate and dispatch one canonical ChunkQuery request."""

    name: ClassVar[str] = "chunk_query_search"
    version: ClassVar[str] = "0.1.0"
    descr: ClassVar[str] = "Search chunks through the canonical typed ChunkQuery contract."
    category: ClassVar[str] = "doc-store"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """Return the strict public request schema."""

        return {
            "type": "object",
            "properties": {
                "query": {
                    **_query_schema(),
                    "description": (
                        "Canonical chunk_metadata_adapter ChunkQuery request, "
                        "including typed filters, mode, thresholds, limits, and controls."
                    ),
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        """Return stable help metadata and remediation guidance."""

        return {
            "name": cls.name,
            "version": cls.version,
            "description": cls.descr,
            "category": cls.category,
            "author": cls.author,
            "email": cls.email,
            "detailed_description": (
                "Accepts only a normalized ChunkQuery and delegates execution to "
                "the G-007 search orchestrator. Results retain ranking, provenance, "
                "and diagnostics supplied by that orchestrator."
            ),
            "parameters": {"query": cls.get_schema()["properties"]["query"]},
            "return_value": {
                "description": "Stable ranked ChunkQuery results with provenance and diagnostics."
            },
            "usage_examples": [
                {"query": {"search_query": "canonical retrieval", "max_results": 10}},
                {"query": {"project": "doc-store", "type": "DocBlock", "tags": ["api"]}},
            ],
            "error_cases": {
                "INVALID_PARAMS": "Provide a canonical ChunkQuery object with no unknown fields.",
                "ORCHESTRATOR_UNAVAILABLE": "Configure the G-007 search_orchestrator execution boundary.",
                "ORCHESTRATOR_RESPONSE_INVALID": "Make G-007 return a serializable ChunkQuery response.",
                "SEARCH_EXECUTION_FAILED": "Inspect G-007 diagnostics and retry with a valid request.",
            },
            "best_practices": [
                "Use chunk_metadata_adapter ChunkQuery fields and typed filter values only.",
                "Do not submit a second query language, SQL, or backend-specific parameters.",
            ],
        }

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Validate the adapter envelope and normalize its canonical query."""

        validated = super().validate_params(params)
        query_value = validated["query"]
        if not isinstance(query_value, Mapping):
            raise AdapterValidationError(
                "INVALID_PARAMS: query must be a canonical ChunkQuery object",
                data={"field": "query", "remediation": "Provide an object matching ChunkQuery."},
            )
        unknown = sorted(set(query_value) - set(ChunkQuery.model_fields))
        if unknown:
            raise AdapterValidationError(
                f"INVALID_PARAMS: unknown ChunkQuery fields: {', '.join(unknown)}",
                data={"unknown_fields": unknown, "remediation": "Use only ChunkQuery fields."},
            )
        if query_value.get("filter_expr") is not None:
            raise AdapterValidationError(
                "INVALID_PARAMS: legacy filter_expr text is not part of the canonical ChunkQuery API",
                data={
                    "field": "filter_expr",
                    "remediation": "Use canonical typed ChunkQuery filter fields instead.",
                },
            )
        try:
            validated["query"] = ChunkQuery.model_validate(dict(query_value))
        except PydanticValidationError as exc:
            raise AdapterValidationError(
                f"INVALID_PARAMS: {exc}",
                data={"remediation": "Provide values accepted by ChunkQuery."},
            ) from exc
        return validated

    async def execute(self, *, query: ChunkQuery, context: Mapping[str, Any] | None = None) -> CommandResult:
        """Delegate exactly once to G-007 and serialize its stable response."""

        runtime_context = dict(context or {})
        orchestrator: SearchOrchestrator | Any = runtime_context.pop("search_orchestrator", None)
        if orchestrator is None:
            return ErrorResult(
                "ORCHESTRATOR_UNAVAILABLE: configure the G-007 search_orchestrator",
                details={"remediation": "Provide context.search_orchestrator."},
            )
        try:
            if callable(orchestrator):
                response = orchestrator(query, **runtime_context)
            else:
                response = orchestrator.execute(query, **runtime_context)
            if inspect.isawaitable(response):
                response = await response
            return CommandResult(data=_serialize_result(response))
        except (TypeError, ValueError) as exc:
            return ErrorResult(
                f"ORCHESTRATOR_RESPONSE_INVALID: {exc}",
                details={"remediation": "Return a serializable G-007 ChunkQuery response."},
            )
        except Exception as exc:
            return ErrorResult(
                f"SEARCH_EXECUTION_FAILED: {exc}",
                details={"remediation": "Inspect G-007 diagnostics and retry the canonical request."},
            )


__all__ = ["ChunkQuerySearchCommand", "SearchOrchestrator"]
