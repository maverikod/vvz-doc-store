"""Public command for the external embedding vectorizer worker."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Protocol

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.commands.result import ErrorResult

from doc_store_server.runtime.vectorization import (
    VectorizationError,
    installed_vectorization_service,
)


class VectorizationBoundary(Protocol):
    async def rebuild(self, **kwargs: Any) -> Mapping[str, Any]: ...


class EmbeddingsRebuildCommand(Command):
    """Rebuild stored chunk embeddings through the external embed-client."""

    name: ClassVar[str] = "embeddings_rebuild"
    version: ClassVar[str] = "0.1.0"
    descr: ClassVar[str] = "Batch-vectorize stored chunks through embed-client."
    category: ClassVar[str] = "doc-store.vectorization"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = True
    vectorization_boundary: ClassVar[VectorizationBoundary | None] = None

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Optional document UUID to vectorize regardless of flags.",
                },
                "all_documents": {
                    "type": "boolean",
                    "description": "Rebuild all active documents instead of only needs_revectorize documents.",
                },
                "document_limit": {
                    "type": "integer",
                    "description": "Optional maximum number of documents to process.",
                },
                "document_batch_size": {
                    "type": "integer",
                    "description": "Number of documents read and committed per vectorizer batch.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Only report selected documents/chunks; do not call embed-client or write vectors.",
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
                "Runs the vectorizer boundary separately from ingestion/chunking. "
                "It reads documents with needs_revectorize flags in document batches, "
                "calls embed-client in configured text batches, writes "
                "semantic_chunk_embeddings in database batches, clears processed flags, "
                "and records vectorizer_activity.jsonl / vectorizer_processed.jsonl / "
                "vectorizer_errors.jsonl events. The activity log is written before "
                "embedding each document and after successful persistence so health can "
                "show the current file while vectorization is still incomplete. "
                "When the embedding service is unavailable the command returns an "
                "embedding_unavailable status instead of crashing and suppresses repeated "
                "unavailable log entries until a later successful batch."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {
                "description": "Status, processed document/chunk counts, selected document ids, and effective embedding metadata."
            },
            "usage_examples": [
                {},
                {"document_limit": 5, "document_batch_size": 2},
                {"all_documents": True, "document_limit": 20},
                {"document_id": "550e8400-e29b-41d4-a716-446655440000"},
            ],
            "error_cases": {
                "VECTORIZATION_BOUNDARY_UNAVAILABLE": "Vectorizer boundary is not configured.",
                "INVALID_PARAMS": "Invalid document_id, document_limit, or document_batch_size.",
                "VECTORIZATION_FAILED": "Unexpected vectorization failure outside normal service-unavailable handling.",
            },
            "best_practices": [
                "Keep this command queued for large corpora.",
                "Use dry_run before all_documents=true rebuilds.",
                "Do not vectorize during chunking; chunking sets flags and this command clears them after successful batches.",
            ],
        }

    async def execute(
        self,
        document_id: str | None = None,
        all_documents: bool = False,
        document_limit: int | None = None,
        document_batch_size: int = 5,
        dry_run: bool = False,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        boundary = None
        if context and context.get("vectorization_boundary") is not None:
            boundary = context["vectorization_boundary"]
        if boundary is None:
            boundary = self.vectorization_boundary or installed_vectorization_service()
        if boundary is None:
            return ErrorResult(
                "VECTORIZATION_BOUNDARY_UNAVAILABLE: vectorization boundary is not configured",
                details={"code": "VECTORIZATION_BOUNDARY_UNAVAILABLE"},
            )
        try:
            return CommandResult(
                data=await boundary.rebuild(
                    document_id=document_id,
                    all_documents=all_documents,
                    document_limit=document_limit,
                    document_batch_size=document_batch_size,
                    dry_run=dry_run,
                )
            )
        except ValueError as exc:
            return ErrorResult(f"INVALID_PARAMS: {exc}", details={"code": "INVALID_PARAMS"})
        except VectorizationError as exc:
            return ErrorResult(
                f"VECTORIZATION_FAILED: {exc}",
                details={"code": "VECTORIZATION_FAILED"},
            )
        except Exception as exc:
            return ErrorResult(
                f"VECTORIZATION_FAILED: {exc}",
                details={"code": "VECTORIZATION_FAILED"},
            )


__all__ = ["EmbeddingsRebuildCommand", "VectorizationBoundary"]
