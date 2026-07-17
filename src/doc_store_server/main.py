"""Adapter-owned runtime entrypoint for the doc-store server."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import logging
import os
from pathlib import Path
from typing import Any

from mcp_proxy_adapter.api.app import create_app
from mcp_proxy_adapter.commands.command_registry import CommandRegistry, registry
from mcp_proxy_adapter.commands.hooks import hooks
from mcp_proxy_adapter.core.server_engine import ServerEngineFactory

from doc_store_server.commands.registration import (
    register_doc_store_commands as register_doc_store_commands,
)
from doc_store_server.commands.chunk_query_search_command import ChunkQuerySearchCommand
from doc_store_server.commands.document_delete_command import DocumentDeleteCommand
from doc_store_server.commands.health_command import DocStoreHealthCommand
from doc_store_server.commands.document_export_command import DocumentExportCommand
from doc_store_server.commands.entity_lifecycle_commands import (
    EntityCreateCommand,
    EntityGetCommand,
    EntityHardDeleteCommand,
    EntityListCommand,
    EntityReferencesCommand,
    EntitySoftDeleteCommand,
    EntityUpdateCommand,
    EntityUndeleteCommand,
)
from doc_store_server.commands.ingestion_commands import (
    DocumentChunkCommand,
    DocumentCreateCommand,
    DocumentUpdateCommand,
)
from doc_store_server.commands.processing_status_command import ProcessingStatusCommand
from doc_store_server.commands.retrieval_commands import (
    ChapterGetCommand,
    DocumentGetCommand,
    ParagraphGetByNumberCommand,
    ParagraphGetCommand,
)
from doc_store_server.commands.text_reconstruction_commands import (
    ChapterTextGetCommand,
    SourceFileReconstructCommand,
)
from doc_store_server.commands.vectorization_command import EmbeddingsRebuildCommand
from doc_store_server.db.health import check_database_health, database_url_from_config
from doc_store_server.ingestion.runtime_boundary import (
    RuntimeIngestionBoundary,
    installed_svo_runtime_chunker,
    installed_runtime_status,
)
from doc_store_server.query.retrieval_boundary import installed_retrieval_boundary
from doc_store_server.query.runtime_boundary import installed_search_orchestrator
from doc_store_server.runtime.document_export import installed_document_export_service
from doc_store_server.runtime.document_service import installed_document_service
from doc_store_server.runtime.entity_lifecycle import installed_entity_lifecycle_service
from doc_store_server.runtime.embedding_config import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_DIRECT_TEXT_MAX_CHARS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_MODEL_VERSION,
    DEFAULT_EMBEDDING_POLL_INTERVAL,
    DEFAULT_EMBEDDING_PORT,
    DEFAULT_EMBEDDING_PROTOCOL,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_EMBEDDING_TIMEOUT,
    DEFAULT_EMBEDDING_WAIT_TIMEOUT,
)
from doc_store_server.runtime.search_config import default_search_config, normalize_search_config
from doc_store_server.runtime.text_reconstruction import installed_text_reconstruction_service
from doc_store_server.runtime.vectorization import installed_vectorization_service
from doc_store_server.config_cli import validate_config_data, validation_errors


ServerConfig = Mapping[str, Any]
logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def default_config_from_env() -> dict[str, Any]:
    """Build the minimal adapter config required for a valid runtime startup."""

    database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    database_connect_timeout = _env_int("DOC_STORE_DATABASE_CONNECT_TIMEOUT", 3)
    config: dict[str, Any] = {
        "server": {
            "host": os.getenv("DOC_STORE_HOST", "0.0.0.0"),
            "port": _env_int("DOC_STORE_PORT", 8000),
            "protocol": os.getenv("DOC_STORE_PROTOCOL", "http"),
            "debug": _env_bool("DOC_STORE_DEBUG", False),
            "log_level": os.getenv("DOC_STORE_LOG_LEVEL", "info"),
        },
        "queue_manager": {
            "enabled": _env_bool("DOC_STORE_QUEUE_ENABLED", True),
            "in_memory": _env_bool("DOC_STORE_QUEUE_IN_MEMORY", True),
        },
        "embedding": _embedding_config_from_env({}),
        "search": default_search_config(),
    }
    if database_url:
        config["database"] = {"url": database_url, "connect_timeout": database_connect_timeout}
    return normalize_search_config(config)


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load adapter config from a JSON file and add runtime secrets from env."""

    path = config_path or os.getenv("DOC_STORE_CONFIG")
    if path:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        config = default_config_from_env()

    database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if database_url:
        database = dict(config.get("database") or {})
        database["url"] = database_url
        database["connect_timeout"] = _env_int(
            "DOC_STORE_DATABASE_CONNECT_TIMEOUT",
            int(database.get("connect_timeout", 3) or 3),
        )
        config["database"] = database
    config = _overlay_embedding_env(config)
    return normalize_search_config(config)


def _embedding_config_from_env(base: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocol": os.getenv("DOC_STORE_EMBEDDING_PROTOCOL", str(base.get("protocol", DEFAULT_EMBEDDING_PROTOCOL))),
        "host": os.getenv("DOC_STORE_EMBEDDING_HOST", str(base.get("host", ""))),
        "port": _env_int("DOC_STORE_EMBEDDING_PORT", int(base.get("port", DEFAULT_EMBEDDING_PORT))),
        "provider": os.getenv("DOC_STORE_EMBEDDING_PROVIDER", str(base.get("provider", DEFAULT_EMBEDDING_PROVIDER))),
        "model": os.getenv("DOC_STORE_EMBEDDING_MODEL", str(base.get("model", DEFAULT_EMBEDDING_MODEL))),
        "model_version": os.getenv(
            "DOC_STORE_EMBEDDING_MODEL_VERSION",
            str(base.get("model_version", DEFAULT_EMBEDDING_MODEL_VERSION)),
        ),
        "dimension": _env_int("DOC_STORE_EMBEDDING_DIMENSION", int(base.get("dimension", DEFAULT_EMBEDDING_DIMENSION))),
        "batch_size": _env_int(
            "DOC_STORE_EMBEDDING_BATCH_SIZE",
            int(base.get("batch_size", DEFAULT_EMBEDDING_BATCH_SIZE)),
        ),
        "timeout": float(os.getenv("DOC_STORE_EMBEDDING_TIMEOUT", str(base.get("timeout", DEFAULT_EMBEDDING_TIMEOUT)))),
        "wait_timeout": _env_int(
            "DOC_STORE_EMBEDDING_WAIT_TIMEOUT",
            int(base.get("wait_timeout", DEFAULT_EMBEDDING_WAIT_TIMEOUT)),
        ),
        "poll_interval": float(
            os.getenv("DOC_STORE_EMBEDDING_POLL_INTERVAL", str(base.get("poll_interval", DEFAULT_EMBEDDING_POLL_INTERVAL)))
        ),
        "direct_text_max_chars": _env_int(
            "DOC_STORE_EMBEDDING_DIRECT_TEXT_MAX_CHARS",
            int(base.get("direct_text_max_chars", DEFAULT_EMBEDDING_DIRECT_TEXT_MAX_CHARS)),
        ),
        "check_hostname": _env_bool("DOC_STORE_EMBEDDING_CHECK_HOSTNAME", bool(base.get("check_hostname", False))),
    }


def _overlay_embedding_env(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("embedding")
    if section is None:
        section = config.get("embedding_client")
    if not isinstance(section, Mapping):
        section = {}
    config["embedding"] = _embedding_config_from_env(section)
    return config


def initialize_command_registry(command_registry: CommandRegistry) -> int:
    """Run the standard custom-command lifecycle for one process."""

    return hooks.execute_custom_commands_hooks(command_registry)


def initialize_main_process() -> int:
    """Initialize the main process using the shared registration hook."""

    return initialize_command_registry(registry)


def initialize_spawned_worker(
    worker_registry: CommandRegistry,
) -> int:
    """Initialize a spawned queue worker using the same registration hook."""

    return initialize_command_registry(worker_registry)


def create_server_application(config: ServerConfig | None = None) -> Any:
    """Create the single application boundary owned by the adapter."""

    initialize_main_process()
    configure_runtime_boundaries(config or {})
    return create_app(
        title="doc-store",
        description="doc-store adapter server",
        version="0.1.59",
        app_config=dict(config or {}),
    )


def configure_runtime_boundaries(config: ServerConfig) -> None:
    """Wire installed-server runtime boundaries into command class defaults."""

    status = installed_runtime_status()
    ingestion = RuntimeIngestionBoundary(
        database_url_from_config(config),
        status,
        installed_svo_runtime_chunker(config),
    )
    search = installed_search_orchestrator(config)
    retrieval = installed_retrieval_boundary(config)
    lifecycle = installed_entity_lifecycle_service(config)
    exporter = installed_document_export_service(dict(config))
    text_reconstruction = installed_text_reconstruction_service(dict(config))
    document_service = installed_document_service(dict(config))
    vectorization = installed_vectorization_service(config)
    DocumentCreateCommand.ingestion_boundary = ingestion
    DocumentUpdateCommand.ingestion_boundary = ingestion
    DocumentChunkCommand.ingestion_boundary = ingestion
    DocumentExportCommand.export_boundary = exporter
    ChapterTextGetCommand.reconstruction_boundary = text_reconstruction
    SourceFileReconstructCommand.reconstruction_boundary = text_reconstruction
    DocumentDeleteCommand.document_service = document_service
    DocumentGetCommand.retrieval_boundary = retrieval
    ChapterGetCommand.retrieval_boundary = retrieval
    ParagraphGetCommand.retrieval_boundary = retrieval
    ParagraphGetByNumberCommand.retrieval_boundary = retrieval
    ProcessingStatusCommand.runtime_status_boundary = status
    ChunkQuerySearchCommand.search_orchestrator = search
    EmbeddingsRebuildCommand.vectorization_boundary = vectorization
    for command in (
        EntityCreateCommand,
        EntityListCommand,
        EntityGetCommand,
        EntityUpdateCommand,
        EntitySoftDeleteCommand,
        EntityUndeleteCommand,
        EntityHardDeleteCommand,
        EntityReferencesCommand,
    ):
        command.lifecycle_boundary = lifecycle
    DocStoreHealthCommand.runtime_config = dict(config)


def run_server(config: ServerConfig | None = None) -> None:
    """Run the adapter server engine with adapter-owned transport handling."""

    runtime_config = dict(config or default_config_from_env())
    _validate_config_before_start(runtime_config)
    _probe_database_without_startup_failure(runtime_config)
    server_config = dict(runtime_config.get("server", {}))
    application = create_server_application(runtime_config)
    engine = ServerEngineFactory.get_engine("hypercorn")
    if engine is None:
        raise RuntimeError("mcp-proxy-adapter hypercorn engine is unavailable")

    engine_config: dict[str, Any] = {
        "host": server_config.get("host", "127.0.0.1"),
        "port": server_config.get("port", 8000),
        "log_level": server_config.get("log_level", "info"),
        "reload": False,
    }
    ssl_config = server_config.get("ssl")
    if isinstance(ssl_config, Mapping):
        if ssl_config.get("cert"):
            engine_config["certfile"] = ssl_config["cert"]
        if ssl_config.get("key"):
            engine_config["keyfile"] = ssl_config["key"]
        if ssl_config.get("ca"):
            engine_config["ca_certs"] = ssl_config["ca"]
        if ssl_config.get("check_hostname") is not None:
            engine_config["check_hostname"] = bool(ssl_config["check_hostname"])

    engine.run_server(
        application,
        engine_config,
    )


def _probe_database_without_startup_failure(config: ServerConfig) -> None:
    """Log database status but never prevent command/help server startup."""

    database = config.get("database") if isinstance(config, Mapping) else None
    connect_timeout = 3
    if isinstance(database, Mapping):
        connect_timeout = int(database.get("connect_timeout", connect_timeout) or connect_timeout)
    status = check_database_health(
        database_url_from_config(config),
        connect_timeout=connect_timeout,
    )
    if status.ok:
        logger.info("Database connectivity ok: %s", status.as_dict())
    elif status.configured:
        logger.warning("Database connectivity unavailable; server stays up: %s", status.as_dict())
    else:
        logger.info("Database URL is not configured; server starts without DB binding")


def _validate_config_before_start(config: ServerConfig) -> None:
    """Validate config before opening the server socket."""

    issues = validate_config_data(dict(config))
    errors = validation_errors(issues)
    if not errors:
        return
    for issue in errors:
        location = ""
        if issue.section:
            location = f" ({issue.section}{'.' + issue.key if issue.key else ''})"
        logger.error("Invalid doc-store config: %s%s", issue.message, location)
    raise SystemExit(1)


def main() -> None:
    """Start doc-store through the installed adapter runtime."""

    parser = argparse.ArgumentParser(description="doc-store adapter server")
    parser.add_argument("--config", type=str, default=os.getenv("DOC_STORE_CONFIG"))
    args = parser.parse_args()
    try:
        run_server(load_config(args.config))
    except SystemExit:
        raise
    except Exception as exc:
        logger.error("doc-store startup failed: %s", exc, exc_info=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
