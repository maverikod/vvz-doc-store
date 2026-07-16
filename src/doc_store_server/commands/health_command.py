"""doc-store health command with database and ingestion metrics."""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any, ClassVar

import psutil

from mcp_proxy_adapter.commands.base import Command
from mcp_proxy_adapter.commands.command_registry import registry
from mcp_proxy_adapter.commands.result import SuccessResult
from mcp_proxy_adapter.core.proxy_registration import get_proxy_registration_status

from doc_store_server.db.health import check_database_health, database_url_from_config
from doc_store_server.ingestion.runtime_boundary import installed_runtime_status
from doc_store_server.runtime.health_metrics import database_metrics, empty_database_metrics
from doc_store_server.runtime.vectorization import installed_vectorization_snapshot


class DocStoreHealthCommand(Command):
    """Return adapter liveness plus doc-store database and worker metrics."""

    name = "health"
    version: ClassVar[str] = "0.1.0"
    descr: ClassVar[str] = "Return doc-store health, database metrics, and worker activity."
    category: ClassVar[str] = "doc-store"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    use_queue: ClassVar[bool] = False
    runtime_config: ClassVar[Mapping[str, Any] | None] = None

    async def execute(self, **_: Any) -> SuccessResult:
        config = dict(self.runtime_config or {})
        database = config.get("database") if isinstance(config.get("database"), Mapping) else {}
        connect_timeout = int(database.get("connect_timeout", 3) or 3) if isinstance(database, Mapping) else 3
        database_url = database_url_from_config(config) or os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
        db_health = check_database_health(database_url, connect_timeout=connect_timeout)
        metrics = database_metrics(database_url, connect_timeout=connect_timeout) if db_health.ok else empty_database_metrics()
        components = _platform_components()
        components["database"] = {"available": db_health.ok, **db_health.as_dict(), **metrics}
        worker = installed_runtime_status().snapshot()
        worker["vectorizer"] = installed_vectorization_snapshot(config)
        components["worker"] = worker
        return SuccessResult(
            {
                "status": "ok" if db_health.ok else "error",
                "version": _adapter_version(),
                "application": {"name": "doc-store", "version": _doc_store_version()},
                "uptime": components["process"]["uptime_seconds"],
                "components": components,
            }
        )

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
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
                "Extends the adapter health command with doc-store database availability, "
                "document and paragraph counts, current ingestion worker activity, per-document "
                "vectorization percentage, recent vectorized-document throughput, and the "
                "current vectorizer file/document when embeddings_rebuild is running."
            ),
            "parameters": {},
            "return_value": {
                "description": "Adapter liveness and doc-store observability metrics."
            },
            "usage_examples": [{}],
            "error_cases": {
                "DATABASE_UNAVAILABLE": "Database status is reported as data and the server remains reachable."
            },
            "best_practices": [
                "Use this command through MCP proxy to verify registration, database availability, and indexing progress.",
                "Treat worker activity as a best-effort process-local and persisted snapshot.",
            ],
        }


def _platform_components() -> dict[str, Any]:
    process = psutil.Process(os.getpid())
    start_time = datetime.fromtimestamp(process.create_time())
    uptime_seconds = (datetime.now() - start_time).total_seconds()
    memory_info = process.memory_info()
    return {
        "system": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "process": {
            "pid": os.getpid(),
            "memory_usage_mb": memory_info.rss / (1024 * 1024),
            "start_time": start_time.isoformat(),
            "uptime_seconds": uptime_seconds,
        },
        "commands": {"registered_count": len(registry.get_all_commands())},
        "proxy_registration": get_proxy_registration_status(),
    }


def _adapter_version() -> str:
    for distribution in ("mcp-proxy-adapter", "mcp_proxy_adapter"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    return "unknown"


def _doc_store_version() -> str:
    try:
        return version("doc-store")
    except PackageNotFoundError:
        return "unknown"


__all__ = ["DocStoreHealthCommand"]
