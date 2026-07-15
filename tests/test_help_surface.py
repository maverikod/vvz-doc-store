"""Live help-surface contracts for the registered doc-store commands."""

from __future__ import annotations

import multiprocessing
from typing import Any, cast

from doc_store_server.commands.registration import DOC_STORE_COMMAND_MANIFEST


EXPECTED_METADATA_FIELDS = {
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
REQUIRED_HELP_FIELDS = {"metadata", "schema", "ai_metadata"}


def _manifest_names() -> frozenset[str]:
    """Derive the application command identities from the central manifest."""

    names = frozenset(entry.command_name for entry in DOC_STORE_COMMAND_MANIFEST)
    assert names
    assert len(names) == len(DOC_STORE_COMMAND_MANIFEST)
    return names


def _custom_command_names(registry: Any) -> frozenset[str]:
    """Return only application commands, excluding adapter built-ins."""

    commands = getattr(registry, "_commands", None)
    command_types = getattr(registry, "_command_types", None)
    assert isinstance(commands, dict)
    assert isinstance(command_types, dict)
    return frozenset(
        name for name in commands if command_types.get(name) == "custom"
    )


def _live_help(registry: Any, expected_names: frozenset[str]) -> dict[str, dict[str, Any]]:
    """Interrogate the adapter-generated help surface for every manifest command."""

    assert _custom_command_names(registry) == expected_names
    help_entries: dict[str, dict[str, Any]] = {}
    for name in expected_names:
        entry = registry.get_command_info(name)
        assert isinstance(entry, dict), f"missing live help for {name}"
        assert REQUIRED_HELP_FIELDS <= set(entry), f"partial live help for {name}"
        help_entries[name] = entry
    return help_entries


def _assert_complete_help_entry(name: str, entry: dict[str, Any]) -> None:
    """Check the complete metadata, schema, and remediation-oriented help contract."""

    metadata = entry["metadata"]
    schema = entry["schema"]
    ai_metadata = entry["ai_metadata"]
    assert isinstance(metadata, dict)
    assert isinstance(schema, dict)
    assert isinstance(ai_metadata, dict)

    assert EXPECTED_METADATA_FIELDS <= set(metadata), name
    assert metadata["name"] == name
    assert isinstance(metadata["description"], str) and metadata["description"]
    assert isinstance(metadata["detailed_description"], str)
    assert isinstance(metadata["parameters"], dict)
    assert metadata["parameters_docs"] == metadata["parameters"]
    assert metadata["summary"] == metadata["description"]
    assert metadata["type"] == "custom"
    assert isinstance(metadata["return_value"], dict) and metadata["return_value"]
    assert isinstance(metadata["usage_examples"], list) and metadata["usage_examples"]
    assert isinstance(metadata["error_cases"], dict) and metadata["error_cases"]
    assert isinstance(metadata["best_practices"], list) and metadata["best_practices"]
    assert all(
        isinstance(error_code, str)
        and error_code
        and isinstance(remediation, str)
        and remediation
        for error_code, remediation in metadata["error_cases"].items()
    )
    assert all(isinstance(practice, str) and practice for practice in metadata["best_practices"])

    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    assert isinstance(schema["required"], list)
    assert set(schema["required"]) <= set(schema["properties"])
    assert isinstance(schema["additionalProperties"], bool)
    assert all(
        isinstance(parameter, dict)
        and isinstance(parameter.get("type"), str)
        and isinstance(parameter.get("description"), str)
        and parameter["description"]
        for parameter in schema["properties"].values()
    )

    assert ai_metadata["name"] == name
    assert ai_metadata["description"] == metadata["description"]
    assert ai_metadata["parameters"] == metadata["parameters"]
    assert ai_metadata["return_value"] == metadata["return_value"]
    assert ai_metadata["usage_examples"] == metadata["usage_examples"]
    assert ai_metadata["error_cases"] == metadata["error_cases"]
    assert ai_metadata["best_practices"] == metadata["best_practices"]


def _spawn_help_worker(result: Any) -> None:
    """Build a genuinely fresh registry in a multiprocessing spawn worker."""

    from mcp_proxy_adapter.commands.command_registry import CommandRegistry

    from doc_store_server.main import initialize_spawned_worker

    worker_registry: Any = CommandRegistry()
    initialize_spawned_worker(worker_registry)
    expected_names = _manifest_names()
    result.put(
        (
            sorted(_custom_command_names(worker_registry)),
            _live_help(worker_registry, expected_names),
        )
    )


def test_live_help_is_complete_and_identical_in_main_and_spawn_worker() -> None:
    """Manifest commands must expose equivalent complete live help in both processes."""

    from mcp_proxy_adapter.commands.command_registry import CommandRegistry

    from doc_store_server.main import initialize_command_registry

    expected_names = _manifest_names()
    main_registry: Any = CommandRegistry()
    initialize_command_registry(main_registry)
    main_help = _live_help(main_registry, expected_names)
    for name, entry in main_help.items():
        _assert_complete_help_entry(name, entry)

    context = multiprocessing.get_context("spawn")
    queue: Any = context.Queue()
    worker = context.Process(target=_spawn_help_worker, args=(queue,))
    worker.start()
    try:
        worker_names, worker_help = cast(tuple[list[str], dict[str, dict[str, Any]]], queue.get(timeout=15))
    finally:
        worker.join(timeout=15)
    assert worker.exitcode == 0
    assert frozenset(worker_names) == expected_names
    assert worker_help == main_help

    for name in expected_names:
        assert worker_help[name]["schema"] == main_help[name]["schema"]
        assert worker_help[name]["metadata"] == main_help[name]["metadata"]
