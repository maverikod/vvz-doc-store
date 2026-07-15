"""Spawn-worker registration contract for doc-store commands."""

from __future__ import annotations

import asyncio
import multiprocessing
from typing import Any, cast

from doc_store_server.commands.registration import (
    DOC_STORE_COMMAND_MANIFEST,
    DOC_STORE_COMMAND_MODULE_MANIFEST,
    DOC_STORE_QUEUED_COMMAND_MODULES,
    CommandManifestEntry,
    register_doc_store_commands,
)


def _manifest_names() -> frozenset[str]:
    names = frozenset(entry.command_name for entry in DOC_STORE_COMMAND_MANIFEST)
    assert names
    assert len(names) == len(DOC_STORE_COMMAND_MANIFEST)
    return names


def _queued_entries() -> tuple[CommandManifestEntry, ...]:
    queued = tuple(entry for entry in DOC_STORE_COMMAND_MANIFEST if entry.execution_mode == "queue")
    assert queued
    return queued


def _custom_commands(registry: Any) -> dict[str, type[Any]]:
    commands = getattr(registry, "_commands", None)
    command_types = getattr(registry, "_command_types", None)
    assert isinstance(commands, dict)
    assert isinstance(command_types, dict)
    return {
        str(name): command
        for name, command in commands.items()
        if command_types.get(name) == "custom"
    }


def _command_contracts(registry: Any) -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    for name, command in _custom_commands(registry).items():
        contracts[name] = {
            "class": command.__name__,
            "module": command.__module__,
            "schema": command.get_schema(),
            "metadata": command.metadata(),
        }
    return contracts


def _spawn_registry_worker(result: Any) -> None:
    from mcp_proxy_adapter.commands.command_registry import CommandRegistry

    from doc_store_server.main import initialize_spawned_worker

    worker_registry: Any = CommandRegistry()
    initialize_spawned_worker(worker_registry)
    result.put(_command_contracts(worker_registry))


def _spawn_execution_worker(result: Any) -> None:
    from mcp_proxy_adapter.commands.command_registry import CommandRegistry

    from doc_store_server.main import initialize_spawned_worker

    worker_registry: Any = CommandRegistry()
    initialize_spawned_worker(worker_registry)
    command_name = _queued_entries()[0].command_name
    command_class = _custom_commands(worker_registry)[command_name]

    async def _test_execute(self: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "command": self.name,
            "payload": kwargs,
            "worker_registry_id": id(worker_registry),
        }

    original_execute = command_class.execute
    command_class.execute = _test_execute
    try:
        command = command_class()
        payload = {"title": "Spawn proof", "source": {"kind": "text"}}
        typed_result = asyncio.run(command.execute(**payload))
    finally:
        command_class.execute = original_execute

    result.put(
        {
            "command_name": command_name,
            "typed_result": typed_result,
            "worker_registry_id": id(worker_registry),
        }
    )


def test_auto_import_manifest_covers_every_application_command_module() -> None:
    assert register_doc_store_commands.__auto_import_modules__ == DOC_STORE_COMMAND_MODULE_MANIFEST
    assert DOC_STORE_COMMAND_MODULE_MANIFEST == tuple(
        dict.fromkeys(entry.import_module for entry in DOC_STORE_COMMAND_MANIFEST)
    )
    assert DOC_STORE_QUEUED_COMMAND_MODULES == tuple(
        dict.fromkeys(entry.import_module for entry in _queued_entries())
    )
    assert set(DOC_STORE_QUEUED_COMMAND_MODULES) <= set(
        register_doc_store_commands.__auto_import_modules__
    )


def test_fresh_spawn_worker_registry_matches_main_process_registry() -> None:
    from mcp_proxy_adapter.commands.command_registry import CommandRegistry

    from doc_store_server.main import initialize_command_registry

    main_registry: Any = CommandRegistry()
    initialize_command_registry(main_registry)
    main_contracts = _command_contracts(main_registry)
    assert frozenset(main_contracts) == _manifest_names()

    context = multiprocessing.get_context("spawn")
    queue: Any = context.Queue()
    worker = context.Process(target=_spawn_registry_worker, args=(queue,))
    worker.start()
    try:
        worker_contracts = cast(dict[str, dict[str, Any]], queue.get(timeout=15))
    finally:
        worker.join(timeout=15)

    assert worker.exitcode == 0
    assert worker_contracts == main_contracts


def test_representative_queued_command_executes_inside_fresh_spawn_worker() -> None:
    context = multiprocessing.get_context("spawn")
    queue: Any = context.Queue()
    worker = context.Process(target=_spawn_execution_worker, args=(queue,))
    worker.start()
    try:
        payload = cast(dict[str, Any], queue.get(timeout=15))
    finally:
        worker.join(timeout=15)

    assert worker.exitcode == 0
    assert payload["command_name"] == _queued_entries()[0].command_name
    assert payload["typed_result"] == {
        "ok": True,
        "command": payload["command_name"],
        "payload": {"title": "Spawn proof", "source": {"kind": "text"}},
        "worker_registry_id": payload["worker_registry_id"],
    }
