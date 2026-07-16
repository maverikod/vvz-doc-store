#!/usr/bin/env python3
"""Import ordered docs files through doc-store-client.

The default mode imports BHLFF theory files named ``7d-NN-*.md`` or
``7d-NNN-*.md`` and stores the extracted number in document/chunk metadata.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import UUID


DEFAULT_DOCS_DIR = Path("/home/vasilyvz/Desktop/Инерция/7d/progs/bhlff/docs/theory/files")
DEFAULT_PROJECT = "Теория ВБП"
DEFAULT_PROJECT_DESCRIPTION = "Теория ВБП: импорт файлов 7d по параграфам."
DEFAULT_FILENAME_PATTERN = r"^7d-(?P<number>\d{2,3})-(?P<title>.+)\.md$"
DEFAULT_CHUNKING_STRATEGY = "paragraph"


@dataclass(frozen=True)
class ImportCandidate:
    path: Path
    relative_path: str
    number: int
    number_raw: str
    title: str
    document_id: str
    source_version_id: str
    content_sha256: str
    body_sha256: str
    source_order: int


class CommandDeliveryAdapter:
    """Keep command delivery and file upload inside mcp-proxy-adapter."""

    def __init__(
        self,
        inner: Any,
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


def _uuid4_from_text(value: str) -> str:
    raw = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(raw)))


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


def _iter_files(root: Path, *, recursive: bool) -> Iterable[Path]:
    paths = root.rglob("*") if recursive else root.glob("*")
    for path in sorted(paths):
        if path.is_file():
            yield path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _body_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _parse_candidates(
    docs_dir: Path,
    *,
    filename_pattern: str,
    project_id: str,
    recursive: bool,
) -> tuple[list[ImportCandidate], list[str]]:
    pattern = re.compile(filename_pattern)
    parsed: list[tuple[int, str, str, str, Path, str, str, str]] = []
    skipped: list[str] = []

    for path in _iter_files(docs_dir, recursive=recursive):
        relative = path.relative_to(docs_dir).as_posix()
        match = pattern.match(path.name)
        if match is None:
            skipped.append(relative)
            continue
        number_raw = match.group("number")
        title = match.groupdict().get("title") or path.stem
        parsed.append(
            (
                int(number_raw),
                number_raw,
                title,
                relative,
                path,
                path.name,
                _file_sha256(path),
                _body_sha256(path),
            )
        )

    parsed.sort(key=lambda item: (item[0], item[5]))
    candidates: list[ImportCandidate] = []
    for source_order, (number, number_raw, title, relative, path, filename, content_sha256, body_sha256) in enumerate(
        parsed,
        start=1,
    ):
        document_id = _uuid4_from_text(f"doc-store:{project_id}:file:{filename}")
        candidates.append(
            ImportCandidate(
                path=path,
                relative_path=relative,
                number=number,
                number_raw=number_raw,
                title=title,
                document_id=document_id,
                source_version_id=f"sha256:{content_sha256}",
                content_sha256=content_sha256,
                body_sha256=body_sha256,
                source_order=source_order,
            )
        )
    return candidates, skipped


def _metadata(
    candidate: ImportCandidate,
    *,
    project: str,
    project_id: str,
    project_description: str,
    chunking_strategy: str,
) -> dict[str, Any]:
    return {
        "project": project,
        "project_id": project_id,
        "project_description": project_description,
        "file_id": candidate.document_id,
        "document_id": candidate.document_id,
        "7d_number": candidate.number,
        "7d_number_raw": candidate.number_raw,
        "source_order": candidate.source_order,
        "source_filename": candidate.path.name,
        "source_relative_path": candidate.relative_path,
        "source_title": candidate.title,
        "source_sha256": candidate.content_sha256,
        "file_sha256": candidate.content_sha256,
        "body_sha256": candidate.body_sha256,
        "checksum_algorithm": "sha256",
        "chunking_strategy": chunking_strategy,
    }


def _add_local_client_path() -> None:
    client_src = Path(__file__).resolve().parents[1] / "doc-store-client" / "src"
    if client_src.is_dir():
        sys.path.insert(0, str(client_src))


def _load_client_symbols() -> tuple[Any, Any, Any, Any, Any, Any]:
    _add_local_client_path()
    from doc_store_client import (  # type: ignore[import-not-found]
        DocStoreClient,
        DocStoreClientError,
        DocumentCreateRequest,
        DocumentRebindRequest,
        DocumentUpdateRequest,
    )
    from mcp_proxy_adapter.client.jsonrpc_client.client import (  # type: ignore[import-not-found]
        JsonRpcClient,
    )

    return (
        DocStoreClient,
        DocStoreClientError,
        DocumentCreateRequest,
        DocumentRebindRequest,
        DocumentUpdateRequest,
        JsonRpcClient,
    )


async def _run(args: argparse.Namespace) -> int:
    docs_dir = args.docs_dir.resolve()
    if not docs_dir.is_dir():
        raise SystemExit(f"docs directory does not exist: {docs_dir}")
    project_id = args.project_id or _uuid4_from_text(f"doc-store:project:{args.project}")

    candidates, skipped = _parse_candidates(
        docs_dir,
        filename_pattern=args.filename_pattern,
        project_id=project_id,
        recursive=args.recursive,
    )
    if args.limit is not None:
        candidates = candidates[: args.limit]

    listing = {
        "docs_dir": str(docs_dir),
        "project": args.project,
        "project_id": project_id,
        "chunking_strategy": args.chunking_strategy,
        "matched": len(candidates),
        "skipped": skipped,
    }
    print(json.dumps({"listing": listing}, ensure_ascii=False, sort_keys=True), flush=True)

    if args.dry_run:
        for candidate in candidates:
            metadata = _metadata(
                candidate,
                project=args.project,
                project_id=project_id,
                project_description=args.project_description,
                chunking_strategy=args.chunking_strategy,
            )
            result = {
                "path": candidate.relative_path,
                "number": candidate.number,
                "number_raw": candidate.number_raw,
                "source_order": candidate.source_order,
                "document_id": candidate.document_id,
                "source_version_id": candidate.source_version_id,
                "metadata": metadata,
                "dry_run": True,
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        print(
            json.dumps(
                {
                    "docs_dir": str(docs_dir),
                    "matched": len(candidates),
                    "imported": 0,
                    "updated": 0,
                    "failed": 0,
                    "dry_run": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 0

    (
        DocStoreClient,
        DocStoreClientError,
        DocumentCreateRequest,
        DocumentRebindRequest,
        DocumentUpdateRequest,
        JsonRpcClient,
    ) = _load_client_symbols()

    adapter_inner = JsonRpcClient(
        protocol=args.protocol,
        host=args.host,
        port=args.port,
        cert=args.cert,
        key=args.key,
        ca=args.ca,
        check_hostname=args.check_hostname,
        timeout=args.timeout,
    )
    adapter = CommandDeliveryAdapter(
        adapter_inner,
        use_websocket_session=args.use_websocket_session,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    client = DocStoreClient(adapter)

    imported = 0
    updated = 0
    failed = 0
    for candidate in candidates:
        metadata = _metadata(
            candidate,
            project=args.project,
            project_id=project_id,
            project_description=args.project_description,
            chunking_strategy=args.chunking_strategy,
        )
        operation = "create"
        try:
            response = await client.create_document(
                DocumentCreateRequest(
                    document_id=candidate.document_id,
                    source_version_id=candidate.source_version_id,
                    chunking_strategy=args.chunking_strategy,
                ),
                source_path=str(candidate.path),
                filename=candidate.relative_path,
            )
            imported += 1
        except (DocStoreClientError, RuntimeError) as exc:
            if not args.update_existing:
                failed += 1
                result = {
                    "path": candidate.relative_path,
                    "number": candidate.number,
                    "source_order": candidate.source_order,
                    "document_id": candidate.document_id,
                    "status": "failed",
                    "error": repr(exc),
                }
                print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
                if args.stop_on_error:
                    raise
                continue
            operation = "update"
            response = await client.update_document(
                DocumentUpdateRequest(
                    document_id=candidate.document_id,
                    source_version_id=candidate.source_version_id,
                    chunking_strategy=args.chunking_strategy,
                ),
                source_path=str(candidate.path),
                filename=candidate.relative_path,
            )
            updated += 1

        rebind = await client.rebind_document(
            DocumentRebindRequest(
                document_id=candidate.document_id,
                project=args.project,
                project_id=project_id,
                project_description=args.project_description,
                document_properties=metadata,
                chunk_properties=metadata,
            )
        )
        result = {
            "path": candidate.relative_path,
            "number": candidate.number,
            "number_raw": candidate.number_raw,
            "source_order": candidate.source_order,
            "document_id": candidate.document_id,
            "source_version_id": candidate.source_version_id,
            "operation": operation,
            "status": response.status,
            "operation_id": response.operation_id,
            "rebind": rebind.outcome,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)

    print(
        json.dumps(
            {
                "docs_dir": str(docs_dir),
                "project": args.project,
                "project_id": project_id,
                "matched": len(candidates),
                "imported": imported,
                "updated": updated,
                "failed": failed,
                "dry_run": args.dry_run,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import ordered 7d theory docs into doc-store.")
    parser.add_argument("--docs-dir", type=Path, default=DEFAULT_DOCS_DIR)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--project-id", help="Stable UUID4 project id. Defaults to a deterministic id from --project.")
    parser.add_argument("--project-description", default=DEFAULT_PROJECT_DESCRIPTION)
    parser.add_argument("--chunking-strategy", default=DEFAULT_CHUNKING_STRATEGY, choices=("paragraph", "sentence", "semantic"))
    parser.add_argument("--filename-pattern", default=DEFAULT_FILENAME_PATTERN)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--update-existing", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--protocol", default=os.getenv("DOC_STORE_CLIENT_PROTOCOL", "https"))
    parser.add_argument("--host", default=os.getenv("DOC_STORE_CLIENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DOC_STORE_CLIENT_PORT", "18080")))
    parser.add_argument("--cert", default=os.getenv("DOC_STORE_CLIENT_CERT"))
    parser.add_argument("--key", default=os.getenv("DOC_STORE_CLIENT_KEY"))
    parser.add_argument("--ca", default=os.getenv("DOC_STORE_CLIENT_CA"))
    parser.add_argument(
        "--check-hostname",
        action="store_true",
        default=os.getenv("DOC_STORE_CLIENT_CHECK_HOSTNAME", "").lower() in {"1", "true", "yes"},
    )
    parser.add_argument("--timeout", type=float, default=float(os.getenv("DOC_STORE_CLIENT_TIMEOUT", "120")))
    parser.add_argument("--poll-interval", type=float, default=float(os.getenv("DOC_STORE_CLIENT_POLL_INTERVAL", "1")))
    parser.add_argument(
        "--use-websocket-session",
        action="store_true",
        default=os.getenv("DOC_STORE_CLIENT_USE_WEBSOCKET_SESSION", "").lower() in {"1", "true", "yes"},
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
