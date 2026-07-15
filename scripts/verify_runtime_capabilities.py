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
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from chunk_metadata_adapter import ChunkQuery
from doc_store_client import (
    DocStoreClient,
    DocStoreClientError,
    DocumentChunkRequest,
    DocumentCreateRequest,
    DocumentRebindRequest,
    SearchResult,
    DocumentGetRequest,
)
from mcp_proxy_adapter.client.jsonrpc_client.client import JsonRpcClient


CHUNKING_STRATEGIES = ("paragraph", "sentence", "semantic")
DEFAULT_SEARCH_VECTOR = [0.75, 0.25]


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
        created.document_id == document_id,
        created.status,
        {"operation_id": created.operation_id, "source_version_id": created.source_version_id},
    )

    rebind = await client.rebind_document(
        DocumentRebindRequest(
            document_id=document_id,
            project=project,
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
        semantic = await _search(
            client,
            project=project,
            scope=scope,
            strategy=strategy,
            embedding=list(DEFAULT_SEARCH_VECTOR),
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
                document_properties={"runtime_verify_run": run_id, "chunking_strategy": strategy},
                chunk_properties={
                    "runtime_verify_scope": scope,
                    "runtime_verify_run": run_id,
                    "chunking_strategy": strategy,
                },
            )
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
        "chunk_query_search",
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

    with tempfile.TemporaryDirectory(prefix="doc-store-runtime-verify-") as temp_root:
        tmpdir = Path(temp_root)
        strategy_runs = []
        for strategy in CHUNKING_STRATEGIES:
            strategy_run = await _verify_strategy(
                client,
                tmpdir=tmpdir,
                strategy=strategy,
                project=args.project,
                scope=scope,
                run_id=run_id,
                vectorization_timeout=args.vectorization_timeout,
                poll_interval=args.poll_interval,
            )
            strategy_runs.append(strategy_run)
            all_checks.extend(strategy_run.checks)

    if strategy_runs:
        all_checks.append(
            await _verify_retrieval_boundary(
                client,
                document_id=strategy_runs[0].document_id,
                strict=args.strict,
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
