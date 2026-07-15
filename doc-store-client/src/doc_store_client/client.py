"""Thin typed facade over an injected ``mcp-proxy-adapter`` client."""

from __future__ import annotations

from dataclasses import is_dataclass
from typing import Any, ClassVar, Mapping, Protocol, TypeVar

from chunk_metadata_adapter import ChunkQuery

from .models import (
    ChapterGetRequest,
    ChapterGetResult,
    DocumentChunkRequest,
    DocumentChunkResult,
    DocumentCreateRequest,
    DocumentCreateResult,
    DocumentDeleteRequest,
    DocumentDeleteResult,
    DocumentGetRequest,
    DocumentGetResult,
    DocumentRebindRequest,
    DocumentRebindResult,
    DocumentUpdateRequest,
    DocumentUpdateResult,
    ParagraphGetRequest,
    ParagraphGetResult,
    ProcessingStatusRequest,
    ProcessingStatusResult,
    SearchResult,
    ServerError,
)

DOC_STORE_COMMANDS: tuple[str, ...] = (
    "echo",
    "long_task",
    "job_status",
    "queue_add_job",
    "queue_start_job",
    "queue_stop_job",
    "queue_delete_job",
    "queue_get_job_status",
    "queue_get_job_logs",
    "queue_list_jobs",
    "queue_health",
    "document_get",
    "chapter_get",
    "paragraph_get",
    "document_create",
    "document_update",
    "document_chunk",
    "document_rebind",
    "processing_status",
    "document_delete",
    "chunk_query_search",
    "help",
    "health",
    "config",
    "reload",
    "settings",
    "load",
    "unload",
    "plugins",
    "transport_management",
    "proxy_registration",
    "roletest",
    "transfer_upload_begin",
    "transfer_upload_status",
    "transfer_upload_complete",
    "transfer_download_begin",
    "transfer_download_status",
)


class JsonRpcClientLike(Protocol):
    """Minimal adapter surface used by the typed facade."""

    async def execute_command_unified(self, command: str, params: Mapping[str, Any]) -> Any:
        """Execute a doc-store command through the adapter."""


class TransferClientLike(JsonRpcClientLike, Protocol):
    """Adapter surface when file-transfer ingestion is used."""

    async def upload_file(
        self,
        source_path: str,
        *,
        filename: str | None = None,
        compression: str = "identity",
        chunk_size: int | None = None,
        on_progress: Any = None,
    ) -> Any:
        """Delegate file transfer to the adapter implementation."""


T = TypeVar("T")


class DocStoreClientError(RuntimeError):
    """Raised when the adapter or server returns a structured command error."""

    def __init__(self, error: ServerError) -> None:
        self.error = error
        super().__init__(error.message)


class DocStoreClient:
    """Typed doc-store operations backed by an injected adapter client."""

    commands: ClassVar[tuple[str, ...]] = DOC_STORE_COMMANDS

    def __init__(self, adapter_client: JsonRpcClientLike) -> None:
        self._adapter_client = adapter_client

    async def call(
        self,
        command: str,
        params: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute any server command through the injected adapter client.

        The adapter owns networking, TLS/mTLS, authentication, queue delivery, and
        retries.  This method only gives callers a stable command-level facade.
        """

        command_name = command.strip()
        if not command_name:
            raise ValueError("command must be non-empty")
        return _unwrap_response(
            await self._adapter_client.execute_command_unified(
                command_name, _merge_params(params, kwargs)
            )
        )

    async def echo(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("echo", params, **kwargs)

    async def long_task(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("long_task", params, **kwargs)

    async def job_status(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("job_status", params, **kwargs)

    async def queue_add_job(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("queue_add_job", params, **kwargs)

    async def queue_start_job(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("queue_start_job", params, **kwargs)

    async def queue_stop_job(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("queue_stop_job", params, **kwargs)

    async def queue_delete_job(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("queue_delete_job", params, **kwargs)

    async def queue_get_job_status(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("queue_get_job_status", params, **kwargs)

    async def queue_get_job_logs(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("queue_get_job_logs", params, **kwargs)

    async def queue_list_jobs(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("queue_list_jobs", params, **kwargs)

    async def queue_health(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("queue_health", params, **kwargs)

    async def document_get(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("document_get", params, **kwargs)

    async def chapter_get(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("chapter_get", params, **kwargs)

    async def paragraph_get(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("paragraph_get", params, **kwargs)

    async def document_create(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("document_create", params, **kwargs)

    async def document_update(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("document_update", params, **kwargs)

    async def document_chunk(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("document_chunk", params, **kwargs)

    async def document_rebind(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("document_rebind", params, **kwargs)

    async def processing_status(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("processing_status", params, **kwargs)

    async def document_delete(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("document_delete", params, **kwargs)

    async def chunk_query_search(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("chunk_query_search", params, **kwargs)

    async def help(
        self,
        cmdname: str | None = None,
        params: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        request = _merge_params(params, kwargs)
        if cmdname is not None:
            if "cmdname" in request:
                raise ValueError("cmdname supplied twice")
            request["cmdname"] = cmdname
        return await self.call("help", request)

    async def health(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("health", params, **kwargs)

    async def config(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("config", params, **kwargs)

    async def reload(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("reload", params, **kwargs)

    async def settings(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("settings", params, **kwargs)

    async def load(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("load", params, **kwargs)

    async def unload(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("unload", params, **kwargs)

    async def plugins(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("plugins", params, **kwargs)

    async def transport_management(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("transport_management", params, **kwargs)

    async def proxy_registration(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("proxy_registration", params, **kwargs)

    async def roletest(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self.call("roletest", params, **kwargs)

    async def transfer_upload_begin(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("transfer_upload_begin", params, **kwargs)

    async def transfer_upload_status(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("transfer_upload_status", params, **kwargs)

    async def transfer_upload_complete(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("transfer_upload_complete", params, **kwargs)

    async def transfer_download_begin(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("transfer_download_begin", params, **kwargs)

    async def transfer_download_status(
        self, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.call("transfer_download_status", params, **kwargs)

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

    async def chunk_document(self, request: DocumentChunkRequest) -> DocumentChunkResult:
        return await self._execute("document_chunk", request.to_params(), DocumentChunkResult)

    async def rebind_document(self, request: DocumentRebindRequest) -> DocumentRebindResult:
        return await self._execute(
            "document_rebind", request.to_params(), DocumentRebindResult
        )

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

    async def upload_file(
        self,
        source_path: str,
        *,
        filename: str | None = None,
        compression: str = "identity",
        chunk_size: int | None = None,
        on_progress: Any = None,
    ) -> Mapping[str, Any]:
        """Upload a file through the injected adapter and return a transfer reference."""

        upload = getattr(self._adapter_client, "upload_file", None)
        if upload is None:
            raise TypeError("adapter client does not expose upload_file")
        receipt = await upload(
            source_path,
            filename=filename,
            compression=compression,
            chunk_size=chunk_size,
            on_progress=on_progress,
        )
        return _completed_transfer_reference(receipt)

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
        transfer_ref = await self.upload_file(source_path, filename=filename)
        params = request.to_params()
        params.pop("raw_text", None)
        params["transferred_file"] = transfer_ref
        return params

    async def _execute(self, command: str, params: Mapping[str, Any], result_type: type[T]) -> T:
        response = await self._adapter_client.execute_command_unified(command, dict(params))
        payload = _unwrap_response(response, expect_mapping=True)
        if hasattr(result_type, "from_payload"):
            return result_type.from_payload(payload)  # type: ignore[attr-defined,no-any-return]
        return result_type(**payload)  # type: ignore[call-arg]


def _merge_params(
    params: Mapping[str, Any] | None,
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    if params is None:
        result: dict[str, Any] = {}
    elif isinstance(params, Mapping):
        result = dict(params)
    else:
        raise TypeError("params must be a mapping")
    duplicate = sorted(set(result).intersection(kwargs))
    if duplicate:
        raise ValueError(f"duplicate parameters: {', '.join(duplicate)}")
    result.update(kwargs)
    return result


def _unwrap_response(response: Any, *, expect_mapping: bool = False) -> Any:
    if isinstance(response, Mapping):
        if _is_adapter_envelope(response):
            return _unwrap_response(response["result"], expect_mapping=expect_mapping)
        if _is_queue_status_payload(response):
            return _unwrap_response(response["result"], expect_mapping=expect_mapping)
        if response.get("success") is False:
            raise DocStoreClientError(_error_from_payload(response))
        data = response.get("data", response)
        if isinstance(data, Mapping) or not expect_mapping:
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
    if isinstance(data, Mapping) or not expect_mapping:
        return data
    raise TypeError("adapter response data must be an object")


def _is_adapter_envelope(response: Mapping[str, Any]) -> bool:
    return "mode" in response and "result" in response


def _is_queue_status_payload(response: Mapping[str, Any]) -> bool:
    result = response.get("result")
    return (
        "job_id" in response
        and "command" in response
        and isinstance(result, Mapping)
    )


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
        values = dict(receipt)
    else:
        transfer_id = getattr(receipt, "transfer_id", None) or getattr(receipt, "id", None)
        values = {
            key: getattr(receipt, key, None)
            for key in (
                "transfer_id",
                "id",
                "filename",
                "path",
                "size_bytes",
                "checksum_algorithm",
                "checksum_value",
                "compression",
                "chunk_size",
                "status",
                "plaintext_size_bytes",
            )
        }
    values["transfer_id"] = transfer_id
    result = {
        key: value
        for key, value in values.items()
        if key != "completed" and value is not None and key != "id"
    }
    if not result:
        raise DocStoreClientError(
            ServerError(code="TRANSFER_REFERENCE_MISSING", message="file transfer reference is missing")
        )
    return result


def _dump_query(query: ChunkQuery) -> Mapping[str, Any]:
    if hasattr(query, "model_dump"):
        return query.model_dump(mode="python", exclude_none=True, exclude_unset=True)
    if is_dataclass(query):
        return {
            key: value
            for key, value in query.__dict__.items()
            if value is not None
        }
    if isinstance(query, Mapping):
        return dict(query)
    raise TypeError("query must be a chunk_metadata_adapter.ChunkQuery")


__all__ = [
    "DOC_STORE_COMMANDS",
    "DocStoreClient",
    "DocStoreClientError",
    "JsonRpcClientLike",
    "TransferClientLike",
]
