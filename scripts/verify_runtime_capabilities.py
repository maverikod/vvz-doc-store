#!/usr/bin/env python3
"""Verify doc-store runtime capabilities through the public client facade.

The script targets a real running doc-store server.  It creates temporary files,
uploads them through ``DocStoreClient.create_document(..., source_path=...)`` for
every supported chunking strategy, and then verifies the observable API surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
import uuid
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from chunk_metadata_adapter import ChunkQuery
from doc_store_client import (
    DocStoreClient,
    DocStoreClientError,
    DocumentChunkRequest,
    DocumentCreateRequest,
    DocumentGetRequest,
    DocumentRebindRequest,
    EntityGetRequest,
    EntityIdsRequest,
    EntityListRequest,
    EntityOwnerTreeRequest,
    EntityReferencesRequest,
    ParagraphGetByNumberRequest,
    SearchResult,
    SemanticChunkMetadataUpdateRequest,
)
from mcp_proxy_adapter.client.jsonrpc_client.client import JsonRpcClient


CHUNKING_STRATEGIES = ("paragraph", "sentence", "semantic")


def _stable_uuid4(value: str) -> str:
    raw = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


async def _embedding_vector(text_value: str) -> list[float]:
    from embed_client import EmbeddingClient

    model = os.getenv("DOC_STORE_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    dimension = int(os.getenv("DOC_STORE_EMBEDDING_DIMENSION", "384"))
    client = EmbeddingClient(
        protocol=os.getenv("DOC_STORE_EMBEDDING_PROTOCOL", "https"),
        host=os.getenv("DOC_STORE_EMBEDDING_HOST", "192.168.254.26"),
        port=int(os.getenv("DOC_STORE_EMBEDDING_PORT", "8001")),
        cert=os.getenv("DOC_STORE_EMBEDDING_CERT") or None,
        key=os.getenv("DOC_STORE_EMBEDDING_KEY") or None,
        ca=os.getenv("DOC_STORE_EMBEDDING_CA") or None,
        check_hostname=os.getenv("DOC_STORE_EMBEDDING_CHECK_HOSTNAME", "").lower()
        in {"1", "true", "yes", "on"},
        timeout=float(os.getenv("DOC_STORE_EMBEDDING_TIMEOUT", "300")),
    )
    response = await client.embed(
        [text_value],
        model=model,
        dimension=dimension,
        wait=True,
        wait_timeout=int(os.getenv("DOC_STORE_EMBEDDING_WAIT_TIMEOUT", "300")),
    )
    results = response.get("results")
    if not isinstance(results, list) or not results:
        raise RuntimeError("embedding response has no results")
    vector = results[0].get("embedding") if isinstance(results[0], Mapping) else results[0]
    if not isinstance(vector, list) or len(vector) != dimension:
        raise RuntimeError("embedding response vector dimension mismatch")
    return [float(item) for item in vector]


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    data: Mapping[str, Any] | None = None


@dataclass
class StrategyRun:
    strategy: str
    document_id: str
    source_version_id: str
    checks: list[Check] = field(default_factory=list)


class CommandDeliveryAdapter:
    """Adapter shim that keeps networking in mcp-proxy-adapter.

    Some deployed servers run without the adapter websocket queue-session
    manager.  In that case ``expect_queue=False`` uses normal command delivery
    while file upload still goes through the adapter transfer client.
    """

    def __init__(
        self,
        inner: JsonRpcClient,
        *,
        use_websocket_session: bool,
        timeout: float,
        poll_interval: float,
    ) -> None:
        self._inner = inner
        self._use_websocket_session = use_websocket_session
        self._timeout = timeout
        self._poll_interval = poll_interval

    async def execute_command_unified(self, command: str, params: Mapping[str, Any]) -> Any:
        if self._use_websocket_session:
            response = await self._inner.execute_command_unified(
                command,
                dict(params),
                auto_poll=True,
                poll_interval=self._poll_interval,
                timeout=self._timeout,
            )
        else:
            response = await self._inner.execute_command_unified(
                command,
                dict(params),
                expect_queue=False,
                auto_poll=True,
                poll_interval=self._poll_interval,
                timeout=self._timeout,
            )
        job_id = _job_id_from_response(response)
        if job_id is None or command == "queue_get_job_status":
            return response
        return await self._poll_job(job_id)

    async def upload_file(
        self,
        source_path: str,
        *,
        filename: str | None = None,
        compression: str = "identity",
        chunk_size: int | None = None,
        on_progress: Any = None,
    ) -> Any:
        return await self._inner.upload_file(
            source_path,
            filename=filename,
            compression=compression,
            chunk_size=chunk_size,
            on_progress=on_progress,
        )

    async def _poll_job(self, job_id: str) -> Any:
        deadline = time.monotonic() + self._timeout
        last: Any = None
        while time.monotonic() < deadline:
            last = await self._inner.execute_command_unified(
                "queue_get_job_status",
                {"job_id": job_id},
                expect_queue=False,
                auto_poll=False,
                timeout=self._timeout,
            )
            status_payload = _response_data(last)
            status = str(status_payload.get("status", "")).lower() if isinstance(status_payload, Mapping) else ""
            if status in {"completed", "complete", "succeeded", "success", "failed", "error"}:
                if status in {"failed", "error"}:
                    raise RuntimeError(f"queued command {job_id} failed: {status_payload}")
                result = status_payload.get("result")
                return result if result is not None else last
            await asyncio.sleep(self._poll_interval)
        raise TimeoutError(f"queued command {job_id} did not finish within {self._timeout}s: {last}")


def _response_data(response: Any) -> Any:
    if not isinstance(response, Mapping):
        return response
    if "mode" in response and "result" in response:
        return _response_data(response["result"])
    if response.get("success") is True and "data" in response:
        return _response_data(response["data"])
    if "result" in response and set(response).issubset({"result", "success", "message"}):
        return _response_data(response["result"])
    return response


def _job_id_from_response(response: Any) -> str | None:
    payload = _response_data(response)
    if isinstance(payload, Mapping):
        job_id = payload.get("job_id")
        if isinstance(job_id, str) and job_id:
            return job_id
        for key in ("data", "result"):
            nested = payload.get(key)
            job_id = _job_id_from_response(nested)
            if job_id:
                return job_id
    return None


def _sample_text(strategy: str, marker: str) -> str:
    if strategy == "paragraph":
        return (
            f"{marker} paragraph alpha stores a first policy paragraph for ordering checks.\n\n"
            f"{marker} paragraph beta mentions full text preview and vectorization.\n\n"
            f"{marker} paragraph gamma closes the file with a final ordered unit.\n"
        )
    if strategy == "sentence":
        return (
            f"{marker} sentence alpha opens the runtime file. "
            f"{marker} sentence beta carries the searchable preview token. "
            f"{marker} sentence gamma keeps unit order visible. "
            f"{marker} sentence delta ends the sentence strategy sample."
        )
    return (
        f"{marker} semantic alpha describes upload, chunking, and persistence. "
        f"{marker} semantic beta groups related meaning for semantic retrieval. "
        f"{marker} semantic gamma records metadata filters and ordering. "
        f"{marker} semantic delta verifies vector search with an embedding query."
    )


def _add_check(
    checks: list[Check],
    name: str,
    ok: bool,
    detail: str = "",
    data: Mapping[str, Any] | None = None,
    *,
    warning: bool = False,
) -> None:
    checks.append(Check(name=name, status="pass" if ok else ("warn" if warning else "fail"), detail=detail, data=data))


def _chunk_meta(hit: Any) -> Mapping[str, Any]:
    chunk = hit.chunk if hasattr(hit, "chunk") else hit.get("chunk", {})
    if not isinstance(chunk, Mapping):
        return {}
    meta = chunk.get("block_meta")
    return meta if isinstance(meta, Mapping) else {}


def _chunk_text(hit: Any) -> str:
    chunk = hit.chunk if hasattr(hit, "chunk") else hit.get("chunk", {})
    if not isinstance(chunk, Mapping):
        return ""
    value = chunk.get("text") or chunk.get("body") or ""
    return str(value)


def _chunk_ordinal(hit: Any) -> int | None:
    chunk = hit.chunk if hasattr(hit, "chunk") else hit.get("chunk", {})
    if not isinstance(chunk, Mapping):
        return None
    for key in ("ordinal", "block_index"):
        value = chunk.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    meta = chunk.get("block_meta")
    if isinstance(meta, Mapping):
        value = meta.get("ordinal") or meta.get("order_index")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _result_hits(result: SearchResult) -> tuple[Any, ...]:
    return tuple(result.results or ())


async def _search(
    client: DocStoreClient,
    *,
    project: str,
    scope: str,
    strategy: str,
    search_query: str | None = None,
    embedding: list[float] | None = None,
    max_results: int = 20,
) -> SearchResult:
    return await client.search(
        ChunkQuery(
            project=project,
            block_meta={
                "runtime_verify_scope": scope,
                "chunking_strategy": strategy,
            },
            search_query=search_query,
            embedding=embedding,
            max_results=max_results,
        )
    )


def _vectorization_status(health: Mapping[str, Any], document_id: str) -> Mapping[str, Any] | None:
    candidates: list[Any] = []
    for key in ("vectorization_by_document", "documents"):
        value = health.get(key)
        if value is not None:
            candidates.append(value)
    metrics = health.get("database") or health.get("db") or {}
    if isinstance(metrics, Mapping):
        for key in ("vectorization_by_document", "documents"):
            value = metrics.get(key)
            if value is not None:
                candidates.append(value)
    components = health.get("components")
    if isinstance(components, Mapping):
        database = components.get("database")
        if isinstance(database, Mapping):
            for key in ("vectorization_by_document", "documents"):
                value = database.get(key)
                if value is not None:
                    candidates.append(value)
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            value = candidate.get(document_id)
            if isinstance(value, Mapping):
                return value
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, Mapping) and item.get("document_id") == document_id:
                    return item
    return None


def _has_vectorization_surface(health: Mapping[str, Any]) -> bool:
    if any(key in health for key in ("vectorization_by_document", "documents")):
        return True
    metrics = health.get("database") or health.get("db")
    if isinstance(metrics, Mapping) and any(
        key in metrics for key in ("vectorization_by_document", "documents")
    ):
        return True
    components = health.get("components")
    if not isinstance(components, Mapping):
        return False
    database = components.get("database")
    return isinstance(database, Mapping) and any(
        key in database for key in ("vectorization_by_document", "documents")
    )


async def _verify_command_help_surface(
    client: DocStoreClient,
    commands: Mapping[str, Any],
) -> list[Check]:
    checks: list[Check] = []
    missing_help: list[str] = []
    missing_schema: list[str] = []

    for command in sorted(commands):
        try:
            help_payload = await client.help(cmdname=command)
        except Exception as exc:
            missing_help.append(f"{command}: {exc!r}")
            continue
        if not isinstance(help_payload, Mapping):
            missing_schema.append(command)
            continue
        if "schema" in help_payload:
            continue
        command_payload = help_payload.get(command)
        if isinstance(command_payload, Mapping) and "schema" in command_payload:
            continue
        missing_schema.append(command)

    _add_check(
        checks,
        "command help schemas",
        not missing_help and not missing_schema,
        f"{len(commands)} command(s)",
        {"missing_help": missing_help, "missing_schema": missing_schema},
    )
    return checks


def _metadata_section(help_payload: Mapping[str, Any], command: str) -> Mapping[str, Any]:
    if isinstance(help_payload.get("metadata"), Mapping):
        return help_payload["metadata"]
    if isinstance(help_payload.get("ai_metadata"), Mapping):
        return help_payload["ai_metadata"]
    command_payload = help_payload.get(command)
    if isinstance(command_payload, Mapping):
        if isinstance(command_payload.get("metadata"), Mapping):
            return command_payload["metadata"]
        if isinstance(command_payload.get("ai_metadata"), Mapping):
            return command_payload["ai_metadata"]
    return {}


async def _verify_metadata_paradigm(client: DocStoreClient) -> list[Check]:
    checks: list[Check] = []
    required_metadata_keys = {
        "name",
        "version",
        "description",
        "category",
        "author",
        "email",
        "detailed_description",
        "parameters",
        "return_value",
        "usage_examples",
        "error_cases",
        "best_practices",
    }
    required_schema_keys = {"type", "properties", "required", "additionalProperties"}
    for command in ("info", "semantic_relations", "semantic_chunk_metadata_update", "corpus_audit"):
        try:
            help_payload = await client.help(cmdname=command)
        except Exception as exc:
            _add_check(checks, f"metadata paradigm {command}", False, repr(exc))
            continue
        if not isinstance(help_payload, Mapping):
            _add_check(checks, f"metadata paradigm {command}", False, "help payload is not an object")
            continue
        metadata = _metadata_section(help_payload, command)
        schema = help_payload.get("schema")
        command_payload = help_payload.get(command)
        if not isinstance(schema, Mapping) and isinstance(command_payload, Mapping):
            schema = command_payload.get("schema")
        schema = schema if isinstance(schema, Mapping) else {}

        missing_metadata = sorted(required_metadata_keys - set(metadata))
        missing_schema = sorted(required_schema_keys - set(schema))
        parameters = metadata.get("parameters")
        examples = metadata.get("usage_examples")
        errors = metadata.get("error_cases")
        practices = metadata.get("best_practices")
        detailed = str(metadata.get("detailed_description", ""))
        return_value = metadata.get("return_value")
        ok = (
            not missing_metadata
            and not missing_schema
            and isinstance(parameters, Mapping)
            and isinstance(examples, list)
            and bool(examples)
            and isinstance(errors, Mapping)
            and bool(errors)
            and isinstance(practices, list)
            and bool(practices)
            and isinstance(return_value, Mapping)
            and len(detailed) >= 80
        )
        _add_check(
            checks,
            f"metadata paradigm {command}",
            ok,
            json.dumps(
                {
                    "missing_metadata": missing_metadata,
                    "missing_schema": missing_schema,
                    "examples": len(examples) if isinstance(examples, list) else 0,
                    "errors": len(errors) if isinstance(errors, Mapping) else 0,
                    "best_practices": len(practices) if isinstance(practices, list) else 0,
                },
                ensure_ascii=False,
            ),
        )
    return checks


async def _verify_info_sections(client: DocStoreClient) -> list[Check]:
    checks: list[Check] = []
    for section in ("semantic_relations", "corpus_audit", "unit_title_editing"):
        try:
            result = await client.call("info", {"section": section})
        except Exception as exc:
            _add_check(checks, f"info section {section}", False, repr(exc))
            continue
        content = result.get("sections", {}).get(section, {}) if isinstance(result, Mapping) else {}
        _add_check(
            checks,
            f"info section {section}",
            isinstance(result, Mapping)
            and result.get("selected_section") == section
            and bool(content.get("content")),
            str(content.get("content", ""))[:200],
        )
    try:
        full = await client.call("info", {})
    except Exception as exc:
        _add_check(checks, "info full technical documentation", False, repr(exc))
    else:
        sections = full.get("sections", {}) if isinstance(full, Mapping) else {}
        reference = full.get("command_reference", {}) if isinstance(full, Mapping) else {}
        serialized = json.dumps(full, ensure_ascii=False, default=str) if isinstance(full, Mapping) else ""
        required_fragments = (
            "semantic_relations",
            "corpus_audit",
            "usage_examples",
            "cosine_distance",
            "exact_duplicates",
            "entity_update",
            "entity_rebind_owner",
            "entity_owner_tree",
            "semantic_chunk_metadata_update",
            "classification",
            "bm25_tokens",
            "version",
            "maintenance",
        )
        _add_check(
            checks,
            "info full technical documentation",
            isinstance(full, Mapping)
            and len(sections) >= 20
            and len(reference) >= 40
            and all(fragment in serialized for fragment in required_fragments),
            json.dumps(
                {
                    "sections": len(sections),
                    "commands": len(reference),
                    "missing_fragments": [
                        fragment for fragment in required_fragments if fragment not in serialized
                    ],
                },
                ensure_ascii=False,
            ),
        )
    return checks


async def _verify_corpus_audit(client: DocStoreClient) -> list[Check]:
    checks: list[Check] = []
    mode_params: tuple[tuple[str, Mapping[str, Any]], ...] = (
        ("unit_title_capabilities", {}),
        ("inventory", {"limit": 3}),
        ("corrections", {"limit": 3}),
        ("conflicts", {"limit": 3}),
        ("exact_duplicates", {"min_length": 80, "limit": 3}),
        ("topics", {"limit": 3}),
    )
    for mode, extra_params in mode_params:
        try:
            result = await client.call("corpus_audit", {"mode": mode, **extra_params})
        except Exception as exc:
            _add_check(checks, f"corpus_audit {mode}", False, repr(exc))
            continue
        diagnostics = result.get("diagnostics", {}) if isinstance(result, Mapping) else {}
        title_capabilities = diagnostics.get("unit_title_editing", {})
        _add_check(
            checks,
            f"corpus_audit {mode}",
            isinstance(result, Mapping)
            and result.get("status") == "ok"
            and result.get("mode") == mode
            and isinstance(title_capabilities, Mapping),
            json.dumps(result.get("pagination", {}), ensure_ascii=False, default=str)
            if isinstance(result, Mapping)
            else "",
        )
    return checks


async def _verify_semantic_relations(client: DocStoreClient) -> list[Check]:
    checks: list[Check] = []
    requests: tuple[tuple[str, Mapping[str, Any]], ...] = (
        (
            "chunk similar distance",
            {
                "level": "chunk",
                "relation": "similar",
                "metric": "cosine_distance",
                "threshold": 0.2,
            },
        ),
        (
            "chunk opposite distance",
            {
                "level": "chunk",
                "relation": "opposite",
                "metric": "cosine_distance",
                "threshold": 0.8,
            },
        ),
        (
            "paragraph similar similarity",
            {
                "level": "paragraph",
                "relation": "similar",
                "metric": "cosine_similarity",
                "threshold": 0.86,
            },
        ),
    )
    for name, params in requests:
        try:
            result = await client.call(
                "semantic_relations",
                {
                    **params,
                    "max_candidates": 40,
                    "max_pairs": 200,
                    "limit": 3,
                    "max_group_size": 4,
                },
            )
        except Exception as exc:
            _add_check(checks, f"semantic_relations {name}", False, repr(exc))
            continue
        _add_check(
            checks,
            f"semantic_relations {name}",
            isinstance(result, Mapping)
            and result.get("status") == "ok"
            and result.get("model")
            and result.get("dimension"),
            json.dumps(
                {
                    "groups": len(result.get("groups", []))
                    if isinstance(result, Mapping)
                    else 0,
                    "model": result.get("model") if isinstance(result, Mapping) else None,
                    "dimension": result.get("dimension")
                    if isinstance(result, Mapping)
                    else None,
                },
                ensure_ascii=False,
                default=str,
            ),
        )
    return checks


async def _wait_vectorized(
    client: DocStoreClient,
    *,
    document_id: str,
    timeout: float,
    interval: float,
) -> tuple[bool, Mapping[str, Any] | None]:
    deadline = time.monotonic() + timeout
    last: Mapping[str, Any] | None = None
    while time.monotonic() < deadline:
        health = await client.health()
        if isinstance(health, Mapping):
            if not _has_vectorization_surface(health):
                return False, None
            last = _vectorization_status(health, document_id)
            if last is not None:
                percent = last.get("percent") or last.get("vectorization_percent")
                vectorized = last.get("vectorized_chunks") or last.get("vectorized")
                total = last.get("total_chunks") or last.get("chunks")
                if percent == 100 or (isinstance(total, int) and total > 0 and vectorized == total):
                    return True, last
        await asyncio.sleep(interval)
    return False, last


async def _verify_strategy(
    client: DocStoreClient,
    *,
    tmpdir: Path,
    strategy: str,
    project: str,
    project_id: str,
    project_description: str,
    scope: str,
    run_id: str,
    vectorization_timeout: float,
    poll_interval: float,
) -> StrategyRun:
    document_id = str(uuid.uuid4())
    source_version_id = f"{run_id}-{strategy}-v1"
    marker = f"runtime_verify_{run_id}_{strategy}"
    path = tmpdir / f"{strategy}.txt"
    path.write_text(_sample_text(strategy, marker), encoding="utf-8")
    checks: list[Check] = []

    created = await client.create_document(
        DocumentCreateRequest(
            document_id=document_id,
            source_version_id=source_version_id,
            chunking_strategy=strategy,
        ),
        source_path=str(path),
        filename=f"{strategy}.txt",
    )
    _add_check(
        checks,
        f"{strategy}: file upload and document_create",
        created.document_id == document_id and created.status in {"completed", "idempotent"},
        created.status,
        {"operation_id": created.operation_id, "source_version_id": created.source_version_id},
    )
    if created.document_id != document_id or created.status not in {"completed", "idempotent"}:
        return StrategyRun(strategy=strategy, document_id=document_id, source_version_id=source_version_id, checks=checks)

    rebind = await client.rebind_document(
        DocumentRebindRequest(
            document_id=document_id,
            project=project,
            project_id=project_id,
            project_description=project_description,
            document_properties={"runtime_verify_run": run_id, "chunking_strategy": strategy},
            chunk_properties={
                "runtime_verify_scope": scope,
                "runtime_verify_run": run_id,
                "chunking_strategy": strategy,
            },
        )
    )
    _add_check(
        checks,
        f"{strategy}: document_rebind",
        rebind.document_id == document_id and rebind.outcome in {"rebound", "updated"},
        rebind.outcome,
        rebind.updated,
    )

    try:
        vectorized_result = await client.call(
            "embeddings_rebuild",
            {"document_id": document_id, "document_batch_size": 1},
        )
        _add_check(
            checks,
            f"{strategy}: embeddings_rebuild",
            isinstance(vectorized_result, Mapping)
            and vectorized_result.get("status") == "ok"
            and int(vectorized_result.get("chunk_count") or 0) > 0,
            json.dumps(vectorized_result, ensure_ascii=False, default=str)[:500],
        )
    except Exception as exc:
        _add_check(checks, f"{strategy}: embeddings_rebuild", False, repr(exc))

    vectorized, vector_status = await _wait_vectorized(
        client,
        document_id=document_id,
        timeout=vectorization_timeout,
        interval=poll_interval,
    )
    _add_check(
        checks,
        f"{strategy}: vectorization health",
        vectorized,
        "health does not expose per-document vectorization" if vector_status is None else "",
        vector_status,
        warning=vector_status is None,
    )

    filtered_hits: tuple[Any, ...] = ()
    try:
        filtered = await _search(client, project=project, scope=scope, strategy=strategy)
        filtered_hits = _result_hits(filtered)
        _add_check(
            checks,
            f"{strategy}: block_meta filter",
            bool(filtered_hits)
            and all(_chunk_meta(hit).get("runtime_verify_scope") == scope for hit in filtered_hits),
            f"{len(filtered_hits)} hit(s)",
        )
    except Exception as exc:
        _add_check(checks, f"{strategy}: block_meta filter", False, repr(exc))

    ordinals = [ordinal for ordinal in (_chunk_ordinal(hit) for hit in filtered_hits) if ordinal is not None]
    _add_check(
        checks,
        f"{strategy}: unit order",
        bool(ordinals) and ordinals == sorted(ordinals),
        json.dumps(ordinals),
    )

    try:
        full_text = await _search(
            client,
            project=project,
            scope=scope,
            strategy=strategy,
            search_query=marker,
            max_results=5,
        )
        full_text_hits = _result_hits(full_text)
        has_preview = any(getattr(hit, "highlights", None) for hit in full_text_hits)
        _add_check(
            checks,
            f"{strategy}: full-text search preview",
            bool(full_text_hits) and has_preview,
            f"{len(full_text_hits)} hit(s)",
        )
    except Exception as exc:
        _add_check(checks, f"{strategy}: full-text search preview", False, repr(exc))

    try:
        query_vector = await _embedding_vector(marker)
        semantic = await _search(
            client,
            project=project,
            scope=scope,
            strategy=strategy,
            embedding=query_vector,
            max_results=5,
        )
        semantic_hits = _result_hits(semantic)
        _add_check(
            checks,
            f"{strategy}: semantic search",
            bool(semantic_hits),
            f"{len(semantic_hits)} hit(s)",
        )
    except Exception as exc:
        _add_check(checks, f"{strategy}: semantic search", False, repr(exc))

    try:
        chunked = await client.chunk_document(DocumentChunkRequest(document_id=document_id))
        _add_check(
            checks,
            f"{strategy}: document_chunk reuses stored strategy",
            chunked.document_id == document_id,
            chunked.status,
            {"operation_id": chunked.operation_id},
        )
    except Exception as exc:
        _add_check(checks, f"{strategy}: document_chunk reuses stored strategy", False, repr(exc))

    try:
        await client.rebind_document(
            DocumentRebindRequest(
                document_id=document_id,
                project=project,
                project_id=project_id,
                project_description=project_description,
                document_properties={"runtime_verify_run": run_id, "chunking_strategy": strategy},
                chunk_properties={
                    "runtime_verify_scope": scope,
                    "runtime_verify_run": run_id,
                    "chunking_strategy": strategy,
                },
            )
        )
        revectorized_result = await client.call(
            "embeddings_rebuild",
            {"document_id": document_id, "document_batch_size": 1},
        )
        _add_check(
            checks,
            f"{strategy}: embeddings_rebuild after rechunk",
            isinstance(revectorized_result, Mapping)
            and revectorized_result.get("status") == "ok"
            and int(revectorized_result.get("chunk_count") or 0) > 0,
            json.dumps(revectorized_result, ensure_ascii=False, default=str)[:500],
        )
        after_rechunk = await _search(client, project=project, scope=scope, strategy=strategy)
        _add_check(
            checks,
            f"{strategy}: searchable after rechunk and rebind",
            bool(_result_hits(after_rechunk)),
            f"{len(_result_hits(after_rechunk))} hit(s)",
        )
    except Exception as exc:
        _add_check(checks, f"{strategy}: searchable after rechunk and rebind", False, repr(exc))

    return StrategyRun(
        strategy=strategy,
        document_id=document_id,
        source_version_id=source_version_id,
        checks=checks,
    )


async def _verify_retrieval_boundary(
    client: DocStoreClient,
    *,
    document_id: str,
    strict: bool,
) -> Check:
    try:
        result = await client.get_document(DocumentGetRequest(document_id=document_id))
    except (DocStoreClientError, Exception) as exc:
        return Check(
            name="document_get retrieval boundary",
            status="fail" if strict else "warn",
            detail=str(exc),
        )
    return Check(
        name="document_get retrieval boundary",
        status="pass",
        detail=str(result),
    )


async def _verify_paragraph_by_number(
    client: DocStoreClient,
    *,
    document_id: str,
) -> Check:
    try:
        result = await client.get_paragraph_by_number(
            ParagraphGetByNumberRequest(document_id=document_id, paragraph_number=2)
        )
    except Exception as exc:
        return Check(
            name="paragraph_get_by_number",
            status="fail",
            detail=repr(exc),
        )
    text = result.text
    if text is None and isinstance(result.value, Mapping):
        text = result.value.get("text")
    return Check(
        name="paragraph_get_by_number",
        status="pass" if result.paragraph_number == 2 and bool(text) else "fail",
        detail=str(text)[:160],
        data={"document_id": result.document_id, "paragraph_number": result.paragraph_number},
    )


async def _verify_lifecycle(
    client: DocStoreClient,
    *,
    document_id: str,
    project: str,
) -> list[Check]:
    checks: list[Check] = []
    try:
        listed = await client.list_entities(
            EntityListRequest(
                entity_type="documents",
                fields=("id", "title", "is_deleted", "block_meta"),
                filters={"id": document_id},
                limit=5,
            )
        )
        _add_check(
            checks,
            "entity_list documents",
            any(item.get("id") == document_id for item in listed.items),
            f"{listed.total} total",
        )
    except Exception as exc:
        _add_check(checks, "entity_list documents", False, repr(exc))

    try:
        entity = await client.get_entity(
            EntityGetRequest(entity_type="documents", entity_id=document_id, fields=("id", "is_deleted"))
        )
        _add_check(checks, "entity_get document", entity.value.get("id") == document_id, str(entity.value))
    except Exception as exc:
        _add_check(checks, "entity_get document", False, repr(exc))

    try:
        refs = await client.get_entity_references(
            EntityReferencesRequest(entity_type="documents", entity_id=document_id)
        )
        _add_check(checks, "entity_references document", isinstance(refs.references, tuple), f"{len(refs.references)} reference(s)")
    except Exception as exc:
        _add_check(checks, "entity_references document", False, repr(exc))

    try:
        tree = await client.get_entity_owner_tree(
            EntityOwnerTreeRequest(entity_id=document_id, entity_type="documents", max_depth=2, max_children_per_node=20)
        )
        _add_check(
            checks,
            "entity_owner_tree document",
            tree.id == document_id
            and tree.tree.get("id") == document_id
            and isinstance(tree.tree.get("children"), (list, tuple)),
            json.dumps(tree.tree, ensure_ascii=False, default=str)[:500],
        )
    except Exception as exc:
        _add_check(checks, "entity_owner_tree document", False, repr(exc))

    try:
        chunks = await client.list_entities(
            EntityListRequest(
                entity_type="semantic_chunks",
                fields=("id", "block_meta"),
                filters={"document_id": document_id},
                limit=1,
            )
        )
        chunk_id = str(chunks.items[0]["id"])
        update_request = SemanticChunkMetadataUpdateRequest(
            chunk_id=chunk_id,
            updates={
                "category": "runtime_verify",
                "tags": ["runtime-verify", "classification:machine"],
                "classification": {
                    "provider": "runtime-pipeline",
                    "model": "fixture-classifier",
                    "model_version": "0",
                    "confidence": 0.9,
                    "evidence": "runtime verification fixture",
                    "review_status": "machine",
                },
            },
            dry_run=True,
        )
        dry_run = await client.update_semantic_chunk_metadata(update_request)
        updated = await client.update_semantic_chunk_metadata(
            SemanticChunkMetadataUpdateRequest(
                chunk_id=chunk_id,
                updates=update_request.updates,
            )
        )
        reread = await client.list_entities(
            EntityListRequest(
                entity_type="semantic_chunks",
                fields=("id", "block_meta"),
                filters={"id": chunk_id},
                limit=1,
            )
        )
        meta = reread.items[0].get("block_meta") if reread.items else {}
        _add_check(
            checks,
            "semantic_chunk_metadata_update",
            dry_run.outcome == "dry_run"
            and updated.updated == 1
            and isinstance(meta, Mapping)
            and meta.get("category") == "runtime_verify"
            and isinstance(meta.get("classification"), Mapping)
            and meta["classification"].get("model") == "fixture-classifier",
            json.dumps({"chunk_id": chunk_id, "meta": meta}, ensure_ascii=False, default=str)[:500],
        )
    except Exception as exc:
        _add_check(checks, "semantic_chunk_metadata_update", False, repr(exc))

    try:
        deleted = await client.soft_delete_entities(EntityIdsRequest(entity_type="documents", ids=(document_id,)))
        hidden = await client.list_entities(EntityListRequest(entity_type="documents", filters={"id": document_id}, limit=5))
        shown = await client.list_entities(
            EntityListRequest(entity_type="documents", filters={"id": document_id}, show_deleted=True, limit=5)
        )
        _add_check(
            checks,
            "entity_soft_delete hides by default",
            deleted.is_deleted is True
            and not any(item.get("id") == document_id for item in hidden.items)
            and any(item.get("id") == document_id and item.get("is_deleted") is True for item in shown.items),
            json.dumps({"updated": deleted.updated, "hidden_total": hidden.total, "shown_total": shown.total}, default=str),
        )
    except Exception as exc:
        _add_check(checks, "entity_soft_delete hides by default", False, repr(exc))

    try:
        restored = await client.undelete_entities(EntityIdsRequest(entity_type="documents", ids=(document_id,)))
        visible = await client.list_entities(EntityListRequest(entity_type="documents", filters={"id": document_id}, limit=5))
        _add_check(
            checks,
            "entity_undelete restores visibility",
            restored.is_deleted is False and any(item.get("id") == document_id for item in visible.items),
            json.dumps({"updated": restored.updated, "visible_total": visible.total}, default=str),
        )
    except Exception as exc:
        _add_check(checks, "entity_undelete restores visibility", False, repr(exc))

    try:
        projects = await client.list_entities(EntityListRequest(entity_type="projects", limit=20))
        _add_check(
            checks,
            "entity_list projects",
            any((item.get("project") or item.get("name")) == project for item in projects.items),
            f"{projects.total} total",
        )
    except Exception as exc:
        _add_check(checks, "entity_list projects", False, repr(exc))
    return checks


async def _verify_hard_delete(client: DocStoreClient, *, document_id: str) -> list[Check]:
    checks: list[Check] = []
    try:
        result = await client.hard_delete_entities(EntityIdsRequest(entity_type="documents", ids=(document_id,)))
        deleted = result.deleted or {}
        missing = False
        try:
            await client.get_entity(EntityGetRequest(entity_type="documents", entity_id=document_id, show_deleted=True))
        except Exception:
            missing = True
        _add_check(
            checks,
            "entity_hard_delete document closure",
            result.outcome == "deleted" and int(deleted.get("documents", 0)) >= 1 and missing,
            json.dumps(deleted, default=str),
        )
    except Exception as exc:
        _add_check(checks, "entity_hard_delete document closure", False, repr(exc))
    return checks


async def _run(args: argparse.Namespace) -> int:
    adapter = JsonRpcClient(
        protocol=args.protocol,
        host=args.host,
        port=args.port,
        token_header=args.token_header,
        token=args.token,
        cert=args.cert,
        key=args.key,
        ca=args.ca,
        check_hostname=args.check_hostname,
        timeout=args.timeout,
    )
    client = DocStoreClient(
        CommandDeliveryAdapter(
            adapter,
            use_websocket_session=args.use_websocket_session,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )
    )
    run_id = args.run_id or uuid.uuid4().hex[:12]
    scope = args.scope or f"runtime-verify-{run_id}"
    project_id = args.project_id or _stable_uuid4(f"doc-store-runtime:{args.project}")
    project_description = args.project_description or f"Runtime verification project {args.project}"
    all_checks: list[Check] = []

    health = await client.health()
    _add_check(
        all_checks,
        "health",
        isinstance(health, Mapping) and str(health.get("status", "")).lower() in {"ok", "healthy"},
        json.dumps(health, ensure_ascii=False, default=str)[:500],
    )

    help_payload = await client.help()
    commands = help_payload.get("commands", {}) if isinstance(help_payload, Mapping) else {}
    required_commands = {
        "document_create",
        "document_chunk",
        "document_rebind",
        "paragraph_get_by_number",
        "entity_list",
        "entity_get",
        "entity_soft_delete",
        "entity_undelete",
        "entity_hard_delete",
        "entity_references",
        "entity_owner_tree",
        "semantic_chunk_metadata_update",
        "chunk_query_search",
        "semantic_relations",
        "corpus_audit",
        "embeddings_rebuild",
        "info",
        "uuid4",
        "health",
        "help",
        "transfer_upload_begin",
        "transfer_upload_complete",
    }
    _add_check(
        all_checks,
        "command surface",
        required_commands.issubset(set(commands)),
        f"{len(commands)} command(s)",
        {"missing": sorted(required_commands - set(commands))},
    )
    all_checks.extend(await _verify_command_help_surface(client, commands))
    all_checks.extend(await _verify_metadata_paradigm(client))
    all_checks.extend(await _verify_info_sections(client))
    all_checks.extend(await _verify_corpus_audit(client))

    with tempfile.TemporaryDirectory(prefix="doc-store-runtime-verify-") as temp_root:
        tmpdir = Path(temp_root)
        strategy_runs = []
        for strategy in CHUNKING_STRATEGIES:
            strategy_run = await _verify_strategy(
                client,
                tmpdir=tmpdir,
                strategy=strategy,
                project=args.project,
                project_id=project_id,
                project_description=project_description,
                scope=scope,
                run_id=run_id,
                vectorization_timeout=args.vectorization_timeout,
                poll_interval=args.poll_interval,
            )
            strategy_runs.append(strategy_run)
            all_checks.extend(strategy_run.checks)

    all_checks.extend(await _verify_semantic_relations(client))

    if strategy_runs:
        all_checks.append(
            await _verify_retrieval_boundary(
                client,
                document_id=strategy_runs[0].document_id,
                strict=args.strict,
            )
        )
        all_checks.append(
            await _verify_paragraph_by_number(
                client,
                document_id=strategy_runs[0].document_id,
            )
        )
        all_checks.extend(
            await _verify_lifecycle(
                client,
                document_id=strategy_runs[0].document_id,
                project=args.project,
            )
        )
    if len(strategy_runs) > 1:
        all_checks.extend(
            await _verify_hard_delete(
                client,
                document_id=strategy_runs[-1].document_id,
            )
        )

    failed = [check for check in all_checks if check.status == "fail"]
    warnings = [check for check in all_checks if check.status == "warn"]
    summary = {
        "status": "fail" if failed else "pass",
        "target": {
            "protocol": args.protocol,
            "host": args.host,
            "port": args.port,
            "websocket_session": args.use_websocket_session,
        },
        "run_id": run_id,
        "project": args.project,
        "scope": scope,
        "documents": [
            {
                "strategy": run.strategy,
                "document_id": run.document_id,
                "source_version_id": run.source_version_id,
            }
            for run in strategy_runs
        ],
        "checks": [check.__dict__ for check in all_checks],
        "failed": len(failed),
        "warnings": len(warnings),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 1 if failed else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a real doc-store server through DocStoreClient file ingestion."
    )
    parser.add_argument("--protocol", default=os.getenv("DOC_STORE_CLIENT_PROTOCOL", "https"))
    parser.add_argument("--host", default=os.getenv("DOC_STORE_CLIENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DOC_STORE_CLIENT_PORT", "18080")))
    parser.add_argument("--cert", default=os.getenv("DOC_STORE_CLIENT_CERT"))
    parser.add_argument("--key", default=os.getenv("DOC_STORE_CLIENT_KEY"))
    parser.add_argument("--ca", default=os.getenv("DOC_STORE_CLIENT_CA"))
    parser.add_argument("--token-header", default=os.getenv("DOC_STORE_CLIENT_TOKEN_HEADER"))
    parser.add_argument("--token", default=os.getenv("DOC_STORE_CLIENT_TOKEN"))
    parser.add_argument(
        "--check-hostname",
        action="store_true",
        default=os.getenv("DOC_STORE_CLIENT_CHECK_HOSTNAME", "").lower() in {"1", "true", "yes"},
    )
    parser.add_argument("--timeout", type=float, default=float(os.getenv("DOC_STORE_CLIENT_TIMEOUT", "180")))
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--vectorization-timeout", type=float, default=120.0)
    parser.add_argument("--use-websocket-session", action="store_true")
    parser.add_argument("--project", default=os.getenv("DOC_STORE_VERIFY_PROJECT", "doc-store-runtime"))
    parser.add_argument("--project-id", default=os.getenv("DOC_STORE_VERIFY_PROJECT_ID"))
    parser.add_argument("--project-description", default=os.getenv("DOC_STORE_VERIFY_PROJECT_DESCRIPTION"))
    parser.add_argument("--scope", default=os.getenv("DOC_STORE_VERIFY_SCOPE"))
    parser.add_argument("--run-id", default=os.getenv("DOC_STORE_VERIFY_RUN_ID"))
    parser.add_argument("--strict", action="store_true", help="Treat known retrieval warnings as failures.")
    return parser


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_run(_parser().parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
