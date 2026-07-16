"""Public command for semantic relation discovery."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.commands.result import ErrorResult

from doc_store_server.runtime.semantic_relations import installed_semantic_relation_service


class SemanticRelationBoundary(Protocol):
    def search(self, **kwargs: Any) -> Mapping[str, Any]: ...


class SemanticRelationsCommand(Command):
    """Discover groups of similar or opposite indexed semantic units."""

    name: ClassVar[str] = "semantic_relations"
    version: ClassVar[str] = "0.1.0"
    descr: ClassVar[str] = "Discover similar or opposite embedded document, file, paragraph, or chunk units."
    category: ClassVar[str] = "doc-store.analysis"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False
    relation_boundary: ClassVar[SemanticRelationBoundary | None] = None

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["document", "file", "paragraph", "chunk"], "description": "Unit level to compare."},
                "relation": {"type": "string", "enum": ["similar", "opposite"], "description": "Find close or distant semantic units."},
                "metric": {"type": "string", "enum": ["cosine_distance", "cosine_similarity"], "description": "Threshold metric semantics."},
                "threshold": {"type": "number", "description": "Optional threshold. Defaults depend on relation and metric."},
                "project": {"type": "string", "description": "Optional project name or project_id from block_meta."},
                "document_id": {"type": "string", "description": "Optional document UUID filter."},
                "source_name": {"type": "string", "description": "Optional document source_name/source_path filter."},
                "seven_d_number": {"type": "integer", "description": "Optional natural 7d NN filter."},
                "include_deleted": {"type": "boolean", "description": "Include deleted chunks/documents."},
                "max_candidates": {"type": "integer", "description": "Maximum candidate embedded chunks to compare."},
                "max_pairs": {"type": "integer", "description": "Maximum candidate pairs to evaluate."},
                "min_group_size": {"type": "integer", "description": "Minimum group size."},
                "max_group_size": {"type": "integer", "description": "Maximum items returned per group."},
                "limit": {"type": "integer", "description": "Maximum groups returned."},
                "offset": {"type": "integer", "description": "Group offset."},
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
                "Compares active stored semantic_chunk_embeddings within a bounded corpus "
                "scope and returns deterministic relation groups. Similar mode selects "
                "distance below, or similarity above, the threshold. Opposite mode selects "
                "distance above, or similarity below, the threshold."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {
                "description": "Stable JSON with status, scope, metric, relation, model metadata, groups, pagination, and diagnostics."
            },
            "usage_examples": [
                {"level": "chunk", "relation": "similar", "metric": "cosine_similarity", "threshold": 0.86},
                {"level": "paragraph", "relation": "opposite", "metric": "cosine_distance", "threshold": 0.9, "project": "7d"},
            ],
            "error_cases": {
                "INVALID_PARAMS": "Invalid level, relation, metric, threshold, limit, offset, or filter.",
                "RELATION_BOUNDARY_UNAVAILABLE": "Database relation boundary is not configured.",
                "RELATION_SEARCH_FAILED": "Runtime relation query failed.",
            },
            "best_practices": [
                "Use max_candidates to keep corpus-wide self-comparison bounded.",
                "Read model, model_version, and dimension in the response diagnostics before comparing runs.",
            ],
        }

    async def execute(
        self,
        level: str = "chunk",
        relation: str = "similar",
        metric: str = "cosine_distance",
        threshold: float | None = None,
        project: str | None = None,
        document_id: str | None = None,
        source_name: str | None = None,
        seven_d_number: int | None = None,
        include_deleted: bool = False,
        max_candidates: int = 300,
        max_pairs: int = 1000,
        min_group_size: int = 2,
        max_group_size: int = 20,
        limit: int = 20,
        offset: int = 0,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        boundary = None
        if context and context.get("semantic_relation_boundary") is not None:
            boundary = context["semantic_relation_boundary"]
        if boundary is None:
            boundary = self.relation_boundary or installed_semantic_relation_service()
        if boundary is None:
            return ErrorResult(
                "RELATION_BOUNDARY_UNAVAILABLE: database relation boundary is not configured",
                details={"code": "RELATION_BOUNDARY_UNAVAILABLE"},
            )
        try:
            return CommandResult(
                data=boundary.search(
                    level=level,
                    relation=relation,
                    metric=metric,
                    threshold=threshold,
                    project=project,
                    document_id=document_id,
                    source_name=source_name,
                    seven_d_number=seven_d_number,
                    include_deleted=include_deleted,
                    max_candidates=max_candidates,
                    max_pairs=max_pairs,
                    min_group_size=min_group_size,
                    max_group_size=max_group_size,
                    limit=limit,
                    offset=offset,
                )
            )
        except ValueError as exc:
            return ErrorResult(f"INVALID_PARAMS: {exc}", details={"code": "INVALID_PARAMS"})
        except Exception as exc:
            return ErrorResult(f"RELATION_SEARCH_FAILED: {exc}", details={"code": "RELATION_SEARCH_FAILED"})


__all__ = ["SemanticRelationsCommand", "SemanticRelationBoundary"]
