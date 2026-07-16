#!/usr/bin/env python3
"""Print paragraph text by document id and 1-based paragraph number."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
from collections.abc import Mapping
from typing import Any

from doc_store_client import DocStoreClient, ParagraphGetByNumberRequest


class CommandAdapter:
    """Keep transport, TLS and command delivery inside mcp-proxy-adapter."""

    def __init__(self, inner: Any, *, timeout: float) -> None:
        self._inner = inner
        self._timeout = timeout

    async def execute_command_unified(self, command: str, params: Mapping[str, Any]) -> Any:
        return await self._inner.execute_command_unified(
            command,
            dict(params),
            expect_queue=False,
            auto_poll=True,
            timeout=self._timeout,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch one paragraph text by document UUID and 1-based paragraph number."
    )
    parser.add_argument("document_id", help="Document UUID4 identifier.")
    parser.add_argument("paragraph_number", type=int, help="1-based paragraph number.")
    parser.add_argument("--source-version", type=int, default=None, help="Optional source_version.")
    parser.add_argument("--json", action="store_true", help="Print the full response as JSON.")
    parser.add_argument("--protocol", default="https", choices=("http", "https"))
    parser.add_argument("--host", default="192.168.254.26")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--token-header", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--cert", default="mtls-certs/mtls_certificates/client/mcp-proxy.crt")
    parser.add_argument("--key", default="mtls-certs/mtls_certificates/client/mcp-proxy.key")
    parser.add_argument("--ca", default="mtls-certs/mtls_certificates/ca/ca.crt")
    parser.add_argument("--check-hostname", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    logging.getLogger("mcp_proxy_adapter").setLevel(logging.WARNING)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from mcp_proxy_adapter.client.jsonrpc_client.client import JsonRpcClient

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
    adapter = adapter
    client = DocStoreClient(CommandAdapter(adapter, timeout=args.timeout))
    result = await client.get_paragraph_by_number(
        ParagraphGetByNumberRequest(
            document_id=args.document_id,
            paragraph_number=args.paragraph_number,
            source_version=args.source_version,
        )
    )
    if args.json:
        print(json.dumps(result.to_payload(), ensure_ascii=False, indent=2, default=str))
    else:
        text = result.text
        if text is None and isinstance(result.value, Mapping):
            text = result.value.get("text")
        print("" if text is None else text)
    return 0


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
