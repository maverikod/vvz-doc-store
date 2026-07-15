"""Adapter-owned runtime entrypoint for the doc-store server."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from mcp_proxy_adapter.api.app import create_app
from mcp_proxy_adapter.commands.command_registry import CommandRegistry, registry
from mcp_proxy_adapter.commands.hooks import hooks
from mcp_proxy_adapter.core.server_engine import ServerEngineFactory

from doc_store_server.commands.registration import register_doc_store_commands


ServerConfig = Mapping[str, Any]


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
    return create_app(
        title="doc-store",
        description="doc-store adapter server",
        version="0.1.0",
        app_config=dict(config or {}),
    )


def run_server(config: ServerConfig | None = None) -> None:
    """Run the adapter server engine with adapter-owned transport handling."""

    server_config = dict((config or {}).get("server", {}))
    application = create_server_application(config)
    engine = ServerEngineFactory.get_engine("hypercorn")
    if engine is None:
        raise RuntimeError("mcp-proxy-adapter hypercorn engine is unavailable")

    engine.run_server(
        application,
        {
            "host": server_config.get("host", "127.0.0.1"),
            "port": server_config.get("port", 8000),
            "log_level": server_config.get("log_level", "info"),
            "reload": False,
        },
    )


def main() -> None:
    """Start doc-store through the installed adapter runtime."""

    parser = argparse.ArgumentParser(description="doc-store adapter server")
    parser.add_argument("--config", type=str, default=None)
    parser.parse_args()
    run_server()


if __name__ == "__main__":
    main()
