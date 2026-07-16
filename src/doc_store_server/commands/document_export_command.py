"""Document export command."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Protocol
from uuid import UUID

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.commands.result import ErrorResult

from doc_store_server.runtime.document_export import installed_document_export_service


class DocumentExportBoundary(Protocol):
    def export_document(self, **kwargs: Any) -> Mapping[str, Any]: ...


class DocumentExportCommand(Command):
    """Export a document to a text file and record the file entity."""

    name = "document_export"
    version: ClassVar[str] = "0.1.0"
    descr: ClassVar[str] = "Export document text to a file and register the file row."
    category: ClassVar[str] = "doc-store.documents"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False
    export_boundary: ClassVar[DocumentExportBoundary | None] = None

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Document UUID4 identifier."},
                "path": {"type": "string", "description": "Output text file path."},
                "file_id": {"type": "string", "description": "Optional UUID4 file row identifier."},
                "overwrite": {"type": "boolean", "description": "Overwrite existing output path."},
            },
            "required": ["document_id", "path"],
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
                "Reads document paragraphs in canonical order, writes a UTF-8 text file, "
                "calculates the full body SHA-256 checksum, and records a files row with "
                "owner_id set to the document id."
            ),
            "parameters": cls.get_schema()["properties"],
            "return_value": {"description": "Exported file row identity and checksum."},
            "usage_examples": [
                {
                    "document_id": "550e8400-e29b-41d4-a716-446655440000",
                    "path": "/tmp/document.txt",
                    "overwrite": True,
                }
            ],
            "error_cases": {
                "INVALID_PARAMS": "Malformed UUID or path.",
                "NOT_FOUND": "Document does not exist.",
                "FILE_EXISTS": "Output path exists and overwrite is false.",
                "EXPORT_BOUNDARY_UNAVAILABLE": "Database export boundary is not configured.",
            },
            "best_practices": ["Use overwrite=false unless replacing a known export artifact."],
        }

    async def execute(
        self,
        document_id: str,
        path: str,
        file_id: str | None = None,
        overwrite: bool = False,
        context: Mapping[str, Any] | None = None,
    ) -> CommandResult | ErrorResult:
        try:
            UUID(str(document_id))
            if file_id is not None:
                UUID(str(file_id))
        except (TypeError, ValueError) as exc:
            return ErrorResult("document_id and file_id must be UUID4 strings", details={"code": "INVALID_PARAMS", "error": str(exc)})
        if not isinstance(path, str) or not path.strip():
            return ErrorResult("path must be a non-empty string", details={"code": "INVALID_PARAMS"})
        boundary = context.get("document_export_boundary") if isinstance(context, Mapping) else None
        if boundary is None:
            boundary = self.export_boundary or installed_document_export_service()
        if boundary is None:
            return ErrorResult("Document export boundary is unavailable.", details={"code": "EXPORT_BOUNDARY_UNAVAILABLE"})
        try:
            return CommandResult(
                data=boundary.export_document(
                    document_id=document_id,
                    path=path,
                    file_id=file_id,
                    overwrite=overwrite,
                )
            )
        except LookupError as exc:
            return ErrorResult(str(exc), details={"code": "NOT_FOUND"})
        except FileExistsError as exc:
            return ErrorResult(str(exc), details={"code": "FILE_EXISTS"})
        except Exception as exc:
            return ErrorResult(str(exc), details={"code": "INTERNAL_ERROR"})


__all__ = ["DocumentExportBoundary", "DocumentExportCommand"]
