"""Public corpus audit command over indexed documents and chunks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.commands.result import ErrorResult

from doc_store_server.runtime.corpus_audit import installed_corpus_audit_service


class CorpusAuditBoundary(Protocol):
    def audit(self, **kwargs: Any) -> Mapping[str, Any]: ...


class CorpusAuditCommand(Command):
    """Analyze indexed corpus structure, corrections, conflicts, duplicates, and topics."""

    name: ClassVar[str] = "corpus_audit"
    version: ClassVar[str] = "0.1.0"
    descr: ClassVar[str] = "Audit indexed documents and chunks for inventory, corrections, conflicts, duplicates, topics, and unit title capabilities."
    category: ClassVar[str] = "doc-store.analysis"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False
    audit_boundary: ClassVar[CorpusAuditBoundary | None] = None

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["inventory", "corrections", "conflicts", "exact_duplicates", "topics", "unit_title_capabilities"],
                    "description": "Audit mode.",
                },
                "project": {"type": "string", "description": "Optional project name or project_id from block_meta."},
                "document_id": {"type": "string", "description": "Optional document UUID filter."},
                "source_name": {"type": "string", "description": "Optional source_name/source_path filter."},
                "seven_d_number": {"type": "integer", "description": "Optional natural 7d NN filter."},
                "markers": {"type": "array", "items": {"type": "string"}, "description": "Optional marker list for corrections/conflicts."},
                "min_length": {"type": "integer", "description": "Minimum normalized text length for duplicate detection."},
                "include_aggregators": {"type": "boolean", "description": "Include 7d-00 style aggregator files in duplicate detection."},
                "include_deleted": {"type": "boolean", "description": "Include deleted documents/chunks."},
                "limit": {"type": "integer", "description": "Maximum returned items/groups."},
                "offset": {"type": "integer", "description": "Offset for returned items/groups."},
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
                "Provides first-class read-only corpus analysis over indexed documents "
                "and semantic chunks. Inventory parses 7d-NN identifiers and detects "
                "missing, duplicate, non-monotonic, and metadata-mismatched numbering. "
                "Corrections and conflicts use configurable marker evidence. Exact "
                "duplicates normalize whitespace and group equal chunk bodies. Topics "
                "summarize document/source ordering. unit_title_capabilities reports "
                "which unit title fields are currently editable through public APIs."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {
                "description": "Stable JSON with status, mode, scope, items, groups, issues, pagination, and diagnostics."
            },
            "usage_examples": [
                {"mode": "inventory", "project": "7d"},
                {"mode": "corrections", "markers": ["корректировка", "уточнение"]},
                {"mode": "exact_duplicates", "min_length": 120, "include_aggregators": False},
                {"mode": "unit_title_capabilities"},
            ],
            "error_cases": {
                "INVALID_PARAMS": "Invalid mode, limit, offset, min_length, or marker list.",
                "CORPUS_AUDIT_BOUNDARY_UNAVAILABLE": "Database audit boundary is not configured.",
                "CORPUS_AUDIT_FAILED": "Runtime corpus audit query failed.",
            },
            "best_practices": [
                "Use inventory before semantic relation search to confirm 7d numbering and source metadata.",
                "Treat conflicts as candidates; model-backed contradiction classification belongs to the evaluator worker.",
            ],
        }

    async def execute(
        self,
        mode: str = "inventory",
        project: str | None = None,
        document_id: str | None = None,
        source_name: str | None = None,
        seven_d_number: int | None = None,
        markers: Sequence[str] | None = None,
        min_length: int = 80,
        include_aggregators: bool = False,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        boundary = None
        if context and context.get("corpus_audit_boundary") is not None:
            boundary = context["corpus_audit_boundary"]
        if boundary is None:
            boundary = self.audit_boundary or installed_corpus_audit_service()
        if boundary is None:
            return ErrorResult(
                "CORPUS_AUDIT_BOUNDARY_UNAVAILABLE: database audit boundary is not configured",
                details={"code": "CORPUS_AUDIT_BOUNDARY_UNAVAILABLE"},
            )
        try:
            return CommandResult(
                data=boundary.audit(
                    mode=mode,
                    project=project,
                    document_id=document_id,
                    source_name=source_name,
                    seven_d_number=seven_d_number,
                    markers=markers,
                    min_length=min_length,
                    include_aggregators=include_aggregators,
                    include_deleted=include_deleted,
                    limit=limit,
                    offset=offset,
                )
            )
        except ValueError as exc:
            return ErrorResult(f"INVALID_PARAMS: {exc}", details={"code": "INVALID_PARAMS"})
        except Exception as exc:
            return ErrorResult(f"CORPUS_AUDIT_FAILED: {exc}", details={"code": "CORPUS_AUDIT_FAILED"})


__all__ = ["CorpusAuditBoundary", "CorpusAuditCommand"]
