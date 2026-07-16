"""Focused contract tests for the adapter-backed doc-store client facade."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from chunk_metadata_adapter import ChunkQuery
from doc_store_client.client import DOC_STORE_COMMANDS, DocStoreClient, DocStoreClientError
from doc_store_client.models import (
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
    EntityGetRequest,
    EntityGetResult,
    EntityIdsRequest,
    EntityLifecycleResult,
    EntityListRequest,
    EntityListResult,
    EntityOwnerTreeRequest,
    EntityOwnerTreeResult,
    EntityReferencesRequest,
    EntityReferencesResult,
    ParagraphGetByNumberRequest,
    ParagraphGetByNumberResult,
    ParagraphGetRequest,
    ParagraphGetResult,
    ProcessingStatusRequest,
    ProcessingStatusResult,
    SearchResult,
    ServerError,
    SemanticChunkMetadataUpdateRequest,
    SemanticChunkMetadataUpdateResult,
)


class FakeAdapter:
    """Small JsonRpcClient-compatible adapter with adapter-owned delivery."""

    def __init__(self, *, queued: bool = False) -> None:
        self.queued = queued
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.execute_count = 0

    async def execute_command_unified(self, command: str, params: dict[str, Any]) -> Any:
        self.execute_count += 1
        self.calls.append((command, params))
        response = self.responses[command]
        if self.queued:
            # The adapter owns queued delivery; the facade receives one final result.
            await self._deliver_queued_result()
        return response

    async def _deliver_queued_result(self) -> None:
        return None

    async def upload_file(self, source_path: str, **kwargs: Any) -> Any:
        self.uploads.append((source_path, kwargs))
        return {
            "completed": True,
            "transfer_id": "transfer-001",
            "filename": kwargs.get("filename"),
            "path": source_path,
            "size_bytes": 123,
            "checksum_algorithm": "sha256",
            "checksum_value": "a" * 64,
            "compression": kwargs.get("compression", "identity"),
            "chunk_size": 1024,
            "status": "uploaded",
        }


def _write_result(command: str) -> dict[str, Any]:
    return {
        command: {
            "status": "queued",
            "operation_id": "op-001",
            "document_id": "doc-001",
            "source_version_id": "version-001",
        }
    }


def test_document_create_and_update_use_exact_commands_and_typed_results() -> None:
    asyncio.run(_test_document_create_and_update_use_exact_commands_and_typed_results())


async def _test_document_create_and_update_use_exact_commands_and_typed_results() -> None:
    adapter = FakeAdapter()
    adapter.responses.update(_write_result("document_create"))
    adapter.responses.update(_write_result("document_update"))
    client = DocStoreClient(adapter)

    created = await client.create_document(
        DocumentCreateRequest(
            document_id="doc-001",
            source_version_id="version-001",
            raw_text="hello",
            chunking_strategy="paragraph",
        )
    )
    updated = await client.update_document(
        DocumentUpdateRequest(
            document_id="doc-001", source_version_id="version-002", raw_text="updated"
        )
    )

    assert isinstance(created, DocumentCreateResult)
    assert isinstance(updated, DocumentUpdateResult)
    assert adapter.calls == [
        (
            "document_create",
            {
                "document_id": "doc-001",
                "source_version_id": "version-001",
                "raw_text": "hello",
                "chunking_strategy": "paragraph",
            },
        ),
        (
            "document_update",
            {
                "document_id": "doc-001",
                "source_version_id": "version-002",
                "raw_text": "updated",
            },
        ),
    ]


def test_document_write_result_preserves_runtime_extra_fields_in_details() -> None:
    result = DocumentCreateResult.from_payload(
        {
            "status": "completed",
            "operation_id": "op-001",
            "document_id": "doc-001",
            "source_version_id": "version-001",
            "chunk_ids": ["chunk-001"],
            "paragraph_ids": ["paragraph-001"],
            "chunking_strategy": "paragraph",
            "source_version": 1,
        }
    )

    assert result.details == {
        "chunk_ids": ["chunk-001"],
        "paragraph_ids": ["paragraph-001"],
        "chunking_strategy": "paragraph",
        "source_version": 1,
    }


@pytest.mark.parametrize(
    ("method_name", "operation_request", "command", "payload", "result_type", "response"),
    [
        (
            "get_processing_status",
            ProcessingStatusRequest(operation_id="op-001", document_id="doc-001"),
            "processing_status",
            {"operation_id": "op-001", "document_id": "doc-001"},
            ProcessingStatusResult,
            {"operation_id": "op-001", "status": "complete", "progress": 1.0},
        ),
        (
            "get_document",
            DocumentGetRequest(document_id="doc-001", source_version=2),
            "document_get",
            {"document_id": "doc-001", "source_version": 2},
            DocumentGetResult,
            {"entity": "document", "identifier": "doc-001", "source_version": 2},
        ),
        (
            "get_chapter",
            ChapterGetRequest(chapter_id="chapter-001"),
            "chapter_get",
            {"chapter_id": "chapter-001"},
            ChapterGetResult,
            {"entity": "chapter", "identifier": "chapter-001"},
        ),
        (
            "get_paragraph",
            ParagraphGetRequest(paragraph_id="paragraph-001"),
            "paragraph_get",
            {"paragraph_id": "paragraph-001"},
            ParagraphGetResult,
            {"entity": "paragraph", "identifier": "paragraph-001"},
        ),
        (
            "get_paragraph_by_number",
            ParagraphGetByNumberRequest(document_id="doc-001", paragraph_number=2),
            "paragraph_get_by_number",
            {"document_id": "doc-001", "paragraph_number": 2},
            ParagraphGetByNumberResult,
            {
                "entity": "paragraph",
                "identifier": "paragraph-002",
                "document_id": "doc-001",
                "paragraph_number": 2,
                "text": "paragraph text",
            },
        ),
        (
            "delete_document",
            DocumentDeleteRequest(document_id="doc-001", version_token="token-001"),
            "document_delete",
            {"document_id": "doc-001", "version_token": "token-001"},
            DocumentDeleteResult,
            {"outcome": "deleted", "document_id": "doc-001"},
        ),
        (
            "chunk_document",
            DocumentChunkRequest(document_id="doc-001", chunking_strategy="sentence"),
            "document_chunk",
            {"document_id": "doc-001", "chunking_strategy": "sentence"},
            DocumentChunkResult,
            {
                "status": "completed",
                "operation_id": "op-001",
                "document_id": "doc-001",
                "source_version_id": "version-001",
            },
        ),
        (
            "list_entities",
            EntityListRequest(entity_type="documents", fields=("id", "title"), limit=2),
            "entity_list",
            {"entity_type": "documents", "fields": ["id", "title"], "limit": 2, "offset": 0, "show_deleted": False},
            EntityListResult,
            {"entity_type": "documents", "items": [{"id": "doc-001"}], "limit": 2, "offset": 0, "total": 1, "show_deleted": False},
        ),
        (
            "get_entity",
            EntityGetRequest(entity_type="documents", entity_id="doc-001"),
            "entity_get",
            {"entity_type": "documents", "entity_id": "doc-001", "show_deleted": False},
            EntityGetResult,
            {"entity_type": "documents", "id": "doc-001", "value": {"id": "doc-001"}},
        ),
        (
            "soft_delete_entities",
            EntityIdsRequest(entity_type="documents", ids=("doc-001",)),
            "entity_soft_delete",
            {"entity_type": "documents", "ids": ["doc-001"]},
            EntityLifecycleResult,
            {"outcome": "updated", "updated": {"documents": 1}, "is_deleted": True},
        ),
        (
            "undelete_entities",
            EntityIdsRequest(entity_type="documents", ids=("doc-001",)),
            "entity_undelete",
            {"entity_type": "documents", "ids": ["doc-001"]},
            EntityLifecycleResult,
            {"outcome": "updated", "updated": {"documents": 1}, "is_deleted": False},
        ),
        (
            "hard_delete_entities",
            EntityIdsRequest(entity_type="documents", ids=("doc-001",)),
            "entity_hard_delete",
            {"entity_type": "documents", "ids": ["doc-001"]},
            EntityLifecycleResult,
            {"outcome": "deleted", "deleted": {"documents": 1}, "blocked": []},
        ),
        (
            "get_entity_references",
            EntityReferencesRequest(entity_type="documents", entity_id="doc-001"),
            "entity_references",
            {"entity_type": "documents", "entity_id": "doc-001"},
            EntityReferencesResult,
            {"entity_type": "documents", "id": "doc-001", "references": []},
        ),
        (
            "get_entity_owner_tree",
            EntityOwnerTreeRequest(entity_id="doc-001", entity_type="documents", max_depth=2),
            "entity_owner_tree",
            {
                "entity_id": "doc-001",
                "entity_type": "documents",
                "max_depth": 2,
                "max_children_per_node": 200,
                "include_deleted": False,
            },
            EntityOwnerTreeResult,
            {
                "entity_type": "documents",
                "id": "doc-001",
                "max_depth": 2,
                "max_children_per_node": 200,
                "include_deleted": False,
                "tree": {"id": "doc-001", "preview": "doc", "children": []},
            },
        ),
        (
            "update_semantic_chunk_metadata",
            SemanticChunkMetadataUpdateRequest(
                chunk_id="550e8400-e29b-41d4-a716-446655440000",
                updates={
                    "category": "theory",
                    "tags": ("weak-model", "machine"),
                    "classification": {
                        "provider": "local",
                        "model": "small-open-source-classifier",
                        "confidence": 0.42,
                    },
                },
                dry_run=True,
            ),
            "semantic_chunk_metadata_update",
            {
                "chunk_id": "550e8400-e29b-41d4-a716-446655440000",
                "updates": {
                    "category": "theory",
                    "tags": ["weak-model", "machine"],
                    "classification": {
                        "provider": "local",
                        "model": "small-open-source-classifier",
                        "confidence": 0.42,
                    },
                },
                "limit": 100,
                "offset": 0,
                "include_deleted": False,
                "dry_run": True,
            },
            SemanticChunkMetadataUpdateResult,
            {
                "outcome": "dry_run",
                "requested": 1,
                "matched": 1,
                "updated": 0,
                "items": [{"chunk_id": "550e8400-e29b-41d4-a716-446655440000"}],
                "dry_run": True,
            },
        ),
        (
            "rebind_document",
            DocumentRebindRequest(
                document_id="doc-001",
                project="doc-store",
                project_id="550e8400-e29b-41d4-a716-446655440000",
                project_description="doc-store project",
                chunk_properties={"scope": "client"},
            ),
            "document_rebind",
            {
                "document_id": "doc-001",
                "project": "doc-store",
                "project_id": "550e8400-e29b-41d4-a716-446655440000",
                "project_description": "doc-store project",
                "chunk_properties": {"scope": "client"},
            },
            DocumentRebindResult,
            {
                "outcome": "rebound",
                "document_id": "doc-001",
                "project": "doc-store",
                "chunk_properties": {"scope": "client"},
                "updated": {"documents": 1, "semantic_chunks": 2},
            },
        ),
    ],
)
def test_operations_convert_typed_requests_and_results(
    method_name: str,
    operation_request: Any,
    command: str,
    payload: dict[str, Any],
    result_type: type[Any],
    response: dict[str, Any],
) -> None:
    asyncio.run(
        _test_operations_convert_typed_requests_and_results(
            method_name, operation_request, command, payload, result_type, response
        )
    )


async def _test_operations_convert_typed_requests_and_results(
    method_name: str,
    operation_request: Any,
    command: str,
    payload: dict[str, Any],
    result_type: type[Any],
    response: dict[str, Any],
) -> None:
    adapter = FakeAdapter()
    adapter.responses[command] = response

    result = await getattr(DocStoreClient(adapter), method_name)(operation_request)

    assert isinstance(result, result_type)
    assert adapter.calls == [(command, payload)]
    assert adapter.execute_count == 1


@pytest.mark.parametrize("queued", [False, True], ids=["immediate", "adapter-queued"])
def test_search_uses_canonical_query_and_adapter_managed_delivery(queued: bool) -> None:
    asyncio.run(_test_search_uses_canonical_query_and_adapter_managed_delivery(queued))


async def _test_search_uses_canonical_query_and_adapter_managed_delivery(queued: bool) -> None:
    adapter = FakeAdapter(queued=queued)
    adapter.responses["chunk_query_search"] = {
        "status": "success",
        "results": [
            {
                "chunk_id": "chunk-001",
                "chunk": {"text": "hello"},
                "rank": 1,
            }
        ],
        "total_results": 1,
    }
    query = ChunkQuery(search_query="hello", max_results=3, min_score=0.4)

    result = await DocStoreClient(adapter).search(query)

    assert isinstance(result, SearchResult)
    assert result.results[0].chunk_id == "chunk-001"
    assert adapter.calls == [
        (
            "chunk_query_search",
            {"query": query.model_dump(mode="python", exclude_none=True, exclude_unset=True)},
        )
    ]
    assert adapter.execute_count == 1


def test_search_omits_unset_chunk_query_defaults() -> None:
    asyncio.run(_test_search_omits_unset_chunk_query_defaults())


async def _test_search_omits_unset_chunk_query_defaults() -> None:
    adapter = FakeAdapter()
    adapter.responses["chunk_query_search"] = {"status": "success", "results": []}

    await DocStoreClient(adapter).search(ChunkQuery(project="doc-store"))

    assert adapter.calls == [("chunk_query_search", {"query": {"project": "doc-store"}})]


def test_search_accepts_runtime_nested_data_response() -> None:
    asyncio.run(_test_search_accepts_runtime_nested_data_response())


async def _test_search_accepts_runtime_nested_data_response() -> None:
    adapter = FakeAdapter()
    adapter.responses["chunk_query_search"] = {
        "status": "success",
        "data": {
            "results": [
                {
                    "chunk_id": "chunk-001",
                    "chunk": {"text": "runtime body"},
                    "rank": 1,
                    "highlights": {"text": ["runtime"]},
                }
            ],
            "total_results": 1,
            "metadata": {"source": "runtime"},
        },
    }

    result = await DocStoreClient(adapter).search(ChunkQuery(search_query="runtime"))

    assert isinstance(result, SearchResult)
    assert result.status == "success"
    assert result.results[0].chunk_id == "chunk-001"
    assert result.results[0].highlights == {"text": ["runtime"]}
    assert result.metadata == {"source": "runtime"}


def test_structured_command_error_becomes_public_error() -> None:
    asyncio.run(_test_structured_command_error_becomes_public_error())


async def _test_structured_command_error_becomes_public_error() -> None:
    adapter = FakeAdapter()
    adapter.responses["document_get"] = {
        "success": False,
        "error": {
            "code": "NOT_FOUND",
            "message": "document is missing",
            "type": "NotFound",
            "details": {"document_id": "doc-404"},
        },
    }

    with pytest.raises(DocStoreClientError) as raised:
        await DocStoreClient(adapter).get_document(DocumentGetRequest(document_id="doc-404"))

    assert isinstance(raised.value.error, ServerError)
    assert raised.value.error == ServerError(
        code="NOT_FOUND",
        message="document is missing",
        type="NotFound",
        details={"document_id": "doc-404"},
    )


def test_server_error_preserves_runtime_extra_fields_in_details() -> None:
    error = ServerError.from_payload(
        {
            "code": "SEARCH_EXECUTION_FAILED",
            "message": "search failed",
            "data": {"reason": "fixture"},
        }
    )

    assert error.details == {"data": {"reason": "fixture"}}


def test_file_transfer_is_delegated_before_ingestion() -> None:
    asyncio.run(_test_file_transfer_is_delegated_before_ingestion())


async def _test_file_transfer_is_delegated_before_ingestion() -> None:
    adapter = FakeAdapter()
    adapter.responses["document_create"] = {
        "mode": "queued",
        "result": {
            "job_id": "job-001",
            "command": "document_create",
            "result": {
                "success": True,
                "data": {
                    "status": "accepted",
                    "operation_id": "op-002",
                    "document_id": "doc-001",
                    "source_version_id": "version-003",
                },
            },
        },
    }
    client = DocStoreClient(adapter)

    result = await client.create_document(
        DocumentCreateRequest(
            document_id="doc-001",
            source_version_id="version-003",
            chunking_strategy="semantic",
        ),
        source_path="/tmp/manual.pdf",
        filename="manual.pdf",
    )

    assert isinstance(result, DocumentCreateResult)
    assert adapter.uploads == [
        (
            "/tmp/manual.pdf",
            {
                "filename": "manual.pdf",
                "compression": "identity",
                "chunk_size": None,
                "on_progress": None,
            },
        )
    ]
    assert adapter.calls == [
        (
            "document_create",
            {
                "document_id": "doc-001",
                "source_version_id": "version-003",
                "chunking_strategy": "semantic",
                "transferred_file": {
                    "transfer_id": "transfer-001",
                    "filename": "manual.pdf",
                    "path": "/tmp/manual.pdf",
                    "size_bytes": 123,
                    "checksum_algorithm": "sha256",
                    "checksum_value": "a" * 64,
                    "compression": "identity",
                    "chunk_size": 1024,
                    "status": "uploaded",
                },
            },
        )
    ]


def test_public_upload_file_returns_adapter_transfer_reference() -> None:
    asyncio.run(_test_public_upload_file_returns_adapter_transfer_reference())


async def _test_public_upload_file_returns_adapter_transfer_reference() -> None:
    adapter = FakeAdapter()
    client = DocStoreClient(adapter)

    reference = await client.upload_file(
        "/tmp/manual.md",
        filename="manual.md",
        compression="gzip",
        chunk_size=4096,
    )

    assert adapter.uploads == [
        (
            "/tmp/manual.md",
            {
                "filename": "manual.md",
                "compression": "gzip",
                "chunk_size": 4096,
                "on_progress": None,
            },
        )
    ]
    assert reference["transfer_id"] == "transfer-001"
    assert reference["filename"] == "manual.md"
    assert reference["compression"] == "gzip"


def test_every_known_server_command_has_simple_facade_method() -> None:
    asyncio.run(_test_every_known_server_command_has_simple_facade_method())


async def _test_every_known_server_command_has_simple_facade_method() -> None:
    adapter = FakeAdapter()
    for command in DOC_STORE_COMMANDS:
        adapter.responses[command] = {"command": command, "ok": True}

    client = DocStoreClient(adapter)
    assert client.commands == DOC_STORE_COMMANDS

    for command in DOC_STORE_COMMANDS:
        method = getattr(client, command)
        result = await method(params={"marker": command})
        assert result == {"command": command, "ok": True}

    assert adapter.calls == [
        (command, {"marker": command})
        for command in DOC_STORE_COMMANDS
    ]


def test_generic_call_merges_params_and_kwargs_without_network_logic() -> None:
    asyncio.run(_test_generic_call_merges_params_and_kwargs_without_network_logic())


async def _test_generic_call_merges_params_and_kwargs_without_network_logic() -> None:
    adapter = FakeAdapter()
    adapter.responses["document_get"] = {"entity": "document", "identifier": "doc-001"}

    result = await DocStoreClient(adapter).call(
        "document_get",
        {"document_id": "doc-001"},
        source_version=2,
    )

    assert result == {"entity": "document", "identifier": "doc-001"}
    assert adapter.calls == [
        (
            "document_get",
            {"document_id": "doc-001", "source_version": 2},
        )
    ]


def test_generic_call_rejects_ambiguous_or_empty_command_params() -> None:
    asyncio.run(_test_generic_call_rejects_ambiguous_or_empty_command_params())


async def _test_generic_call_rejects_ambiguous_or_empty_command_params() -> None:
    client = DocStoreClient(FakeAdapter())

    with pytest.raises(ValueError, match="command must be non-empty"):
        await client.call(" ")
    with pytest.raises(ValueError, match="duplicate parameters"):
        await client.call("document_get", {"document_id": "doc-001"}, document_id="doc-002")


def test_client_facade_has_no_transport_or_server_implementation() -> None:
    package_root = Path(__file__).parents[1] / "src" / "doc_store_client"
    forbidden_imports = {
        "doc_store_server",
        "requests",
        "httpx",
        "websocket",
        "aiohttp",
    }
    forbidden_names = {"authenticate", "tls", "retry", "poll", "websocket"}
    typed_facade_methods = {
        "create_document",
        "update_document",
        "chunk_document",
        "rebind_document",
        "get_processing_status",
        "get_document",
        "get_chapter",
        "get_paragraph",
        "get_paragraph_by_number",
        "delete_document",
        "list_entities",
        "get_entity",
        "soft_delete_entities",
        "undelete_entities",
        "hard_delete_entities",
        "get_entity_references",
        "get_entity_owner_tree",
        "update_semantic_chunk_metadata",
        "search",
    }
    public_facade_methods = typed_facade_methods | set(DOC_STORE_COMMANDS) | {
        "call",
        "upload_file",
    }

    for path in package_root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(alias.name in forbidden_imports for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden_imports
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in forbidden_names, f"{path.name}:{node.name}"

    client_tree = ast.parse((package_root / "client.py").read_text())
    facade = next(
        node
        for node in client_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DocStoreClient"
    )
    public_methods = {
        node.name
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    assert public_methods == public_facade_methods
    assert "execute_command" not in public_methods
