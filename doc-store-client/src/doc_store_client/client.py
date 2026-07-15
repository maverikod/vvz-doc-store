"""Thin typed facade over an injected ``mcp-proxy-adapter`` client."""

from __future__ import annotations

from dataclasses import is_dataclass
from typing import Any, Mapping, Protocol, TypeVar

from chunk_metadata_adapter import ChunkQuery

from .models import (
    ChapterGetRequest,
    ChapterGetResult,
    DocumentCreateRequest,
    DocumentCreateResult,
    DocumentDeleteRequest,
    DocumentDeleteResult,
    DocumentGetRequest,
    DocumentGetResult,
    DocumentUpdateRequest,
    DocumentUpdateResult,
    ParagraphGetRequest,
    ParagraphGetResult,
    ProcessingStatusRequest,
    ProcessingStatusResult,
    SearchResult,
    ServerError,
)


class JsonRpcClientLike(Protocol):
    """Minimal adapter surface used by the typed facade."""

    async def execute_command_unified(self, command: str, params: Mapping[str, Any]) -> Any:
        """Execute a doc-store command through the adapter."""


class TransferClientLike(JsonRpcClientLike, Protocol):
    """Adapter surface when file-transfer ingestion is used."""

    async def upload_file(self, source_path: str, **kwargs: Any) -> Any:
        """Delegate file transfer to the adapter implementation."""


T = TypeVar("T")


class DocStoreClientError(RuntimeError):
    """Raised when the adapter or server returns a structured command error."""

    def __init__(self, error: ServerError) -> None:
        self.error = error
        super().__init__(error.message)


class DocStoreClient:
    """Typed doc-store operations backed by an injected adapter client."""

    def __init__(self, adapter_client: JsonRpcClientLike) -> None:
        self._adapter_client = adapter_client

    async def create_document(
        self,
        request: DocumentCreateRequest,
        *,
        source_path: str | None = None,
        filename: str | None = None,
    ) -> DocumentCreateResult:
        params = await self._write_params(request, source_path=source_path, filename=filename)
        return await self._execute("document_create", params, DocumentCreateResult)

    async def update_document(
        self,
        request: DocumentUpdateRequest,
        *,
        source_path: str | None = None,
        filename: str | None = None,
    ) -> DocumentUpdateResult:
        params = await self._write_params(request, source_path=source_path, filename=filename)
        return await self._execute("document_update", params, DocumentUpdateResult)

    async def get_processing_status(
        self, request: ProcessingStatusRequest
    ) -> ProcessingStatusResult:
        return await self._execute(
            "processing_status", request.to_params(), ProcessingStatusResult
        )

    async def get_document(self, request: DocumentGetRequest) -> DocumentGetResult:
        return await self._execute("document_get", request.to_params(), DocumentGetResult)

    async def get_chapter(self, request: ChapterGetRequest) -> ChapterGetResult:
        return await self._execute("chapter_get", request.to_params(), ChapterGetResult)

    async def get_paragraph(self, request: ParagraphGetRequest) -> ParagraphGetResult:
        return await self._execute("paragraph_get", request.to_params(), ParagraphGetResult)

    async def delete_document(self, request: DocumentDeleteRequest) -> DocumentDeleteResult:
        return await self._execute("document_delete", request.to_params(), DocumentDeleteResult)

    async def search(self, query: ChunkQuery) -> SearchResult:
        return await self._execute("chunk_query_search", {"query": _dump_query(query)}, SearchResult)

    async def _write_params(
        self,
        request: DocumentCreateRequest | DocumentUpdateRequest,
        *,
        source_path: str | None,
        filename: str | None,
    ) -> dict[str, Any]:
        if source_path is None:
            if request.raw_text is None and request.transferred_file is None:
                raise ValueError("raw_text, transferred_file, or source_path is required")
            return request.to_params()
        upload = getattr(self._adapter_client, "upload_file", None)
        if upload is None:
            raise TypeError("adapter client does not expose upload_file")
        receipt = await upload(source_path, filename=filename)
        transfer_ref = _completed_transfer_reference(receipt)
        return {
            "document_id": request.document_id,
            "source_version_id": request.source_version_id,
            "transferred_file": transfer_ref,
        }

    async def _execute(self, command: str, params: Mapping[str, Any], result_type: type[T]) -> T:
        response = await self._adapter_client.execute_command_unified(command, dict(params))
        payload = _unwrap_response(response)
        if hasattr(result_type, "from_payload"):
            return result_type.from_payload(payload)  # type: ignore[no-any-return]
        return result_type(**payload)  # type: ignore[call-arg]


def _unwrap_response(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        if response.get("success") is False:
            raise DocStoreClientError(_error_from_payload(response))
        data = response.get("data", response)
        if isinstance(data, Mapping):
            return data
    data = getattr(response, "data", response)
    success = getattr(response, "success", True)
    if success is False:
        raise DocStoreClientError(
            ServerError(
                code=str(getattr(response, "code", "SERVER_ERROR")),
                message=str(getattr(response, "error", "command failed")),
                details=getattr(response, "details", None),
            )
        )
    if isinstance(data, Mapping):
        return data
    raise TypeError("adapter response data must be an object")


def _error_from_payload(payload: Mapping[str, Any]) -> ServerError:
    details = payload.get("details")
    error = payload.get("error")
    if isinstance(error, Mapping):
        values = dict(error)
        values.setdefault("code", str(payload.get("code", "SERVER_ERROR")))
        values.setdefault("message", str(values.get("message", "command failed")))
        return ServerError.from_payload(values)
    return ServerError(
        code=str(payload.get("code", "SERVER_ERROR")),
        message=str(error or "command failed"),
        details=details if isinstance(details, Mapping) else None,
    )


def _completed_transfer_reference(receipt: Any) -> Mapping[str, Any]:
    completed = (
        receipt.get("completed")
        if isinstance(receipt, Mapping)
        else getattr(receipt, "completed", False)
    )
    if completed is not True:
        raise DocStoreClientError(
            ServerError(code="TRANSFER_INCOMPLETE", message="file transfer did not complete")
        )
    if isinstance(receipt, Mapping):
        transfer_id = receipt.get("transfer_id") or receipt.get("id")
        path = receipt.get("path")
    else:
        transfer_id = getattr(receipt, "transfer_id", None) or getattr(receipt, "id", None)
        path = getattr(receipt, "path", None)
    result = {key: value for key, value in {"transfer_id": transfer_id, "path": path}.items() if value}
    if not result:
        raise DocStoreClientError(
            ServerError(code="TRANSFER_REFERENCE_MISSING", message="file transfer reference is missing")
        )
    return result


def _dump_query(query: ChunkQuery) -> Mapping[str, Any]:
    if hasattr(query, "model_dump"):
        return query.model_dump(mode="python", exclude_none=True)
    if is_dataclass(query):
        return {
            key: value
            for key, value in query.__dict__.items()
            if value is not None
        }
    if isinstance(query, Mapping):
        return dict(query)
    raise TypeError("query must be a chunk_metadata_adapter.ChunkQuery")


__all__ = ["DocStoreClient", "DocStoreClientError", "JsonRpcClientLike", "TransferClientLike"]
