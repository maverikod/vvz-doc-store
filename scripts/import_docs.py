#!/usr/bin/env python3
"""Import files from a docs directory through doc-store-client."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable
from uuid import UUID

from doc_store_client import DocStoreClient, DocumentCreateRequest
from mcp_proxy_adapter.client.jsonrpc_client.client import JsonRpcClient


DEFAULT_SUFFIXES = (".md", ".markdown", ".txt", ".rst")


def _uuid4_from_text(value: str) -> str:
    raw = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(raw)))


def _iter_files(root: Path, suffixes: tuple[str, ...]) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path


def _source_version_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _run(args: argparse.Namespace) -> int:
    docs_dir = args.docs_dir.resolve()
    if not docs_dir.is_dir():
        raise SystemExit(f"docs directory does not exist: {docs_dir}")

    adapter = JsonRpcClient(
        protocol=args.protocol,
        host=args.host,
        port=args.port,
        cert=args.cert,
        key=args.key,
        ca=args.ca,
        check_hostname=args.check_hostname,
        timeout=args.timeout,
    )
    client = DocStoreClient(adapter)

    imported = 0
    for path in _iter_files(docs_dir, tuple(args.suffix)):
        relative = path.relative_to(docs_dir).as_posix()
        document_id = _uuid4_from_text(f"doc-store:docs:{relative}")
        source_version_id = _source_version_id(path)
        if args.dry_run:
            result = {
                "path": relative,
                "document_id": document_id,
                "source_version_id": source_version_id,
                "dry_run": True,
            }
        else:
            response = await client.create_document(
                DocumentCreateRequest(
                    document_id=document_id,
                    source_version_id=source_version_id,
                ),
                source_path=str(path),
                filename=relative,
            )
            result = {
                "path": relative,
                "document_id": document_id,
                "source_version_id": source_version_id,
                "status": response.status,
                "operation_id": response.operation_id,
            }
        imported += 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))

    print(json.dumps({"imported": imported, "docs_dir": str(docs_dir)}, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import docs files into doc-store.")
    parser.add_argument("--docs-dir", type=Path, default=Path("docs"))
    parser.add_argument("--suffix", action="append", default=list(DEFAULT_SUFFIXES))
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
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
