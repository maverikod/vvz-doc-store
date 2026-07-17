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
    schema.setdefault("properties", {}).update(
        {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": "Server-side page size alias for max_results.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100000,
                "description": "Zero-based result offset for sequential scans.",
            },
        }
    )
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
    search_orchestrator: ClassVar[SearchOrchestrator | Any | None] = None

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
                        "including typed filters, mode, thresholds, limits, and controls. "
                        "The server also accepts limit as an alias for max_results and "
                        "offset for ordered paging."
                    ),
                },
                "semantic_refinement": {
                    "type": "object",
                    "description": "Optional hierarchy-aware semantic controls outside ChunkQuery.",
                    "properties": {
                        "enabled": {"type": "boolean", "description": "Enable hierarchy-aware refinement."},
                        "threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Minimum cosine similarity."},
                        "candidate_limit": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "Primary cross-level candidate limit."},
                        "result_limit": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "Final result window size N."},
                        "diagnostics": {"type": "boolean", "description": "Include refinement diagnostics."},
                    },
                    "additionalProperties": False,
                },
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
                "and diagnostics supplied by that orchestrator. For semantic text "
                "search, send search_query with hybrid_search=true and bm25_weight=0; "
                "the server obtains the query embedding through embed-client before "
                "executing the semantic branch. Hierarchy-aware semantic refinement "
                "uses the separate semantic_refinement command parameter, not "
                "extra ChunkQuery fields. For ordered corpus scans, send "
                "block_meta filters with limit/max_results and offset."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {
                "description": "Stable ranked ChunkQuery results with provenance and diagnostics."
            },
            "usage_examples": [
                {"query": {"search_query": "canonical retrieval", "max_results": 10}},
                {"query": {"block_meta": {"source_name": "7d-55-Периодический_закон_Менделеева.md"}, "limit": 100, "offset": 100}},
                {"query": {"search_query": "semantic concept", "hybrid_search": True, "bm25_weight": 0.0, "semantic_weight": 1.0, "max_results": 10}},
                {"query": {"search_query": "semantic concept", "hybrid_search": True, "bm25_weight": 0.0, "semantic_weight": 1.0}, "semantic_refinement": {"enabled": True, "threshold": 0.45, "candidate_limit": 80, "result_limit": 10}},
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
                "Use limit plus offset to scan a large filtered corpus page by page.",
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
        extension_fields = {"limit", "offset"}
        unknown = sorted(set(query_value) - set(ChunkQuery.model_fields) - extension_fields)
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
        refinement = validated.get("semantic_refinement")
        if refinement is not None and not isinstance(refinement, Mapping):
            raise AdapterValidationError(
                "INVALID_PARAMS: semantic_refinement must be an object",
                data={"field": "semantic_refinement"},
            )
        return validated

    async def execute(
        self,
        *,
        query: ChunkQuery,
        semantic_refinement: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        """Delegate exactly once to G-007 and serialize its stable response."""

        runtime_context = dict(context or {})
        if semantic_refinement is not None:
            runtime_context["semantic_refinement"] = dict(semantic_refinement)
        orchestrator: SearchOrchestrator | Any = runtime_context.pop("search_orchestrator", None)
        if orchestrator is None:
            orchestrator = self.search_orchestrator
        if orchestrator is None:
            from doc_store_server.query.runtime_boundary import installed_search_orchestrator

            orchestrator = installed_search_orchestrator()
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
