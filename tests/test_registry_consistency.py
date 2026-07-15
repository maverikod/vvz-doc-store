"""Registry consistency checks for the explicit doc-store command manifest."""

from __future__ import annotations

import asyncio
import multiprocessing
from dataclasses import replace
from typing import Any, cast

import pytest

from doc_store_server.commands.registration import (
    DOC_STORE_COMMAND_MANIFEST,
    CommandManifestEntry,
)
from doc_store_server.server_manager import RegistryConsistencyError, ServerManager


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


def _help_entries(registry: Any) -> dict[str, dict[str, Any]]:
    return {
        name: registry.get_command_info(name)
        for name in sorted(_custom_commands(registry))
    }


def _contract_view(registry: Any) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "class": command.__name__,
            "module": command.__module__,
            "execution_mode": "queue" if command.use_queue else "sync",
            "schema": command.get_schema(),
            "metadata": command.metadata(),
            "help": registry.get_command_info(name),
        }
        for name, command in sorted(_custom_commands(registry).items())
    }


def _spawn_registry_worker(result: Any) -> None:
    from mcp_proxy_adapter.commands.command_registry import CommandRegistry

    from doc_store_server.main import initialize_spawned_worker

    worker_registry: Any = CommandRegistry()
    initialize_spawned_worker(worker_registry)
    result.put(_contract_view(worker_registry))


def _spawn_execution_worker(result: Any) -> None:
    from mcp_proxy_adapter.commands.command_registry import CommandRegistry

    from doc_store_server.main import initialize_spawned_worker

    worker_registry: Any = CommandRegistry()
    initialize_spawned_worker(worker_registry)
    command_name = _queued_entries()[0].command_name
    command_class = _custom_commands(worker_registry)[command_name]

    async def _test_execute(self: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "command": self.name,
            "payload": kwargs,
        }

    original_execute = command_class.execute
    command_class.execute = _test_execute
    try:
        typed_result = asyncio.run(
            command_class().execute(
                document_id="550e8400-e29b-41d4-a716-446655440000",
                source_version_id="source-v1",
                raw_text="Registry proof",
            )
        )
    finally:
        command_class.execute = original_execute

    result.put({"command": command_name, "result": typed_result})


@pytest.fixture
def main_registry() -> Any:
    from mcp_proxy_adapter.commands.command_registry import CommandRegistry

    from doc_store_server.main import initialize_command_registry

    registry: Any = CommandRegistry()
    initialize_command_registry(registry)
    return registry


def test_main_registry_and_live_help_match_explicit_manifest(main_registry: Any) -> None:
    commands = _custom_commands(main_registry)
    assert frozenset(commands) == _manifest_names()

    for entry in DOC_STORE_COMMAND_MANIFEST:
        command = commands[entry.command_name]
        assert command is entry.command_class
        assert command.__module__ == entry.import_module
        assert ("queue" if command.use_queue else "sync") == entry.execution_mode
        assert entry.metadata_identity == f"{command.__name__}.metadata"
        assert entry.schema_identity == f"{command.__name__}.get_schema"

        schema = command.get_schema()
        metadata = command.metadata()
        help_entry = main_registry.get_command_info(entry.command_name)

        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        assert set(schema["required"]) <= set(schema["properties"])
        assert schema["additionalProperties"] is False
        assert help_entry["schema"] == schema
        assert help_entry["metadata"]["name"] == entry.command_name
        assert help_entry["metadata"]["version"] == metadata["version"]
        assert help_entry["metadata"]["description"] == metadata["description"]
        assert help_entry["metadata"]["category"] == metadata["category"]
        assert help_entry["metadata"]["author"] == metadata["author"]
        assert help_entry["metadata"]["email"] == metadata["email"]
        assert help_entry["metadata"]["detailed_description"] == metadata["detailed_description"]
        assert help_entry["metadata"]["parameters"] == metadata["parameters"]
        assert help_entry["metadata"]["return_value"] == metadata["return_value"]
        assert help_entry["metadata"]["usage_examples"] == metadata["usage_examples"]
        assert help_entry["metadata"]["error_cases"] == metadata["error_cases"]
        assert help_entry["metadata"]["best_practices"] == metadata["best_practices"]

    ServerManager.validate_registry_consistency(ServerManager, main_registry)


def test_fresh_spawn_registry_matches_main_registry_and_help(main_registry: Any) -> None:
    context = multiprocessing.get_context("spawn")
    queue: Any = context.Queue()
    worker = context.Process(target=_spawn_registry_worker, args=(queue,))
    worker.start()
    try:
        worker_view = cast(dict[str, dict[str, Any]], queue.get(timeout=15))
    finally:
        worker.join(timeout=15)

    assert worker.exitcode == 0
    assert worker_view == _contract_view(main_registry)

    worker_registry_pairs = [
        (name, _custom_commands(main_registry)[name])
        for name in sorted(_custom_commands(main_registry))
    ]
    ServerManager.validate_registry_consistency(
        ServerManager,
        main_registry,
        help_view=_help_entries(main_registry),
        worker_view=worker_registry_pairs,
    )


def test_registry_consistency_reports_stable_diagnostics_for_drift(main_registry: Any) -> None:
    commands = _custom_commands(main_registry)
    first_name = DOC_STORE_COMMAND_MANIFEST[0].command_name
    second_name = DOC_STORE_COMMAND_MANIFEST[1].command_name

    cases: dict[str, tuple[Any, str]] = {
        "missing": (
            {name: command for name, command in commands.items() if name != first_name},
            "missing",
        ),
        "unexpected": ({**commands, "unexpected": object}, "unexpected"),
        "duplicate": (
            [(first_name, commands[first_name]), (first_name, commands[first_name])],
            "duplicate",
        ),
        "class mismatch": ({**commands, first_name: object}, "command class mismatch"),
        "help missing": ({name: None for name in commands}, "help missing"),
        "help schema mismatch": (
            {
                **_help_entries(main_registry),
                second_name: {
                    **main_registry.get_command_info(second_name),
                    "schema": {"type": "object", "properties": {}, "required": []},
                },
            },
            "schema mismatch",
        ),
    }

    for expected, (view, diagnostic_fragment) in cases.items():
        kwargs: dict[str, Any] = {}
        registry_view: Any = view
        if expected.startswith("help"):
            registry_view = commands
            kwargs["help_view"] = view
        with pytest.raises(RegistryConsistencyError) as error:
            ServerManager.validate_registry_consistency(
                ServerManager, registry_view, **kwargs
            )
        assert any(
            diagnostic_fragment in diagnostic for diagnostic in error.value.diagnostics
        )


def test_manifest_identity_drift_is_detected(main_registry: Any) -> None:
    drifted = (
        replace(DOC_STORE_COMMAND_MANIFEST[0], metadata_identity="Wrong.metadata"),
        *DOC_STORE_COMMAND_MANIFEST[1:],
    )
    from doc_store_server.server_manager import _RegistryConsistency

    with pytest.raises(RegistryConsistencyError) as error:
        _RegistryConsistency(drifted).validate(main_registry)

    assert any("metadata identity mismatch" in item for item in error.value.diagnostics)


def test_representative_queued_command_executes_inside_spawn_worker() -> None:
    context = multiprocessing.get_context("spawn")
    queue: Any = context.Queue()
    worker = context.Process(target=_spawn_execution_worker, args=(queue,))
    worker.start()
    try:
        payload = cast(dict[str, Any], queue.get(timeout=15))
    finally:
        worker.join(timeout=15)

    assert worker.exitcode == 0
    assert payload == {
        "command": _queued_entries()[0].command_name,
        "result": {
            "success": True,
            "command": _queued_entries()[0].command_name,
            "payload": {
                "document_id": "550e8400-e29b-41d4-a716-446655440000",
                "source_version_id": "source-v1",
                "raw_text": "Registry proof",
            },
        },
    }


def test_registry_consistency_preserves_project_structure_boundaries() -> None:
    source = __file__
    assert source.endswith("tests/test_registry_consistency.py")
    forbidden_terms = tuple(
        ".".join(parts)
        for parts in (
            ("sqlalchemy", "create_engine"),
            ("psycopg", "connect"),
        )
    ) + tuple(name + "(" for name in ("FastAPI", "APIRouter"))
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert not any(term in text for term in forbidden_terms)
