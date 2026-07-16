"""Executable contract tests for the explicit doc-store command manifest."""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from doc_store_server.commands import registration
from doc_store_server.commands.chunk_query_search_command import ChunkQuerySearchCommand
from doc_store_server.commands.document_delete_command import DocumentDeleteCommand
from doc_store_server.commands.document_export_command import DocumentExportCommand
from doc_store_server.commands.document_rebind_command import DocumentRebindCommand
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
from doc_store_server.commands.health_command import DocStoreHealthCommand
from doc_store_server.commands.info import InfoCommand
from doc_store_server.commands.ingestion_commands import (
    DocumentChunkCommand,
    DocumentCreateCommand,
    DocumentUpdateCommand,
)
from doc_store_server.commands.retrieval_commands import (
    ChapterGetCommand,
    DocumentGetCommand,
    ParagraphGetByNumberCommand,
    ParagraphGetCommand,
)
from doc_store_server.commands.uuid4_command import Uuid4Command


ROOT = Path(__file__).resolve().parents[1]
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


class RecordingRegistry:
    """Small registry double that records only the public adapter call."""

    def __init__(self) -> None:
        self.calls: list[tuple[type[Any], str]] = []

    def register(self, command_class: type[Any], command_type: str) -> None:
        self.calls.append((command_class, command_type))


def _manifest_rows() -> list[tuple[str, type[Any], str, str, str, str]]:
    return [
        (
            entry.command_name,
            entry.command_class,
            entry.import_module,
            entry.execution_mode,
            entry.metadata_identity,
            entry.schema_identity,
        )
        for entry in registration.DOC_STORE_COMMAND_MANIFEST
    ]


def _assert_exact_registration(
    calls: list[tuple[type[Any], str]],
    manifest: tuple[registration.CommandManifestEntry, ...],
) -> None:
    expected = [(entry.command_class, "custom") for entry in manifest]
    assert calls == expected
    assert len(calls) == len(set(calls))


def test_manifest_has_exact_command_identity_and_registration_shape() -> None:
    expected = [
        (
            "health",
            DocStoreHealthCommand,
            "doc_store_server.commands.health_command",
            "sync",
            "DocStoreHealthCommand.metadata",
            "DocStoreHealthCommand.get_schema",
        ),
        (
            "info",
            InfoCommand,
            "doc_store_server.commands.info",
            "sync",
            "InfoCommand.metadata",
            "InfoCommand.get_schema",
        ),
        (
            "uuid4",
            Uuid4Command,
            "doc_store_server.commands.uuid4_command",
            "sync",
            "Uuid4Command.metadata",
            "Uuid4Command.get_schema",
        ),
        (
            "document_get",
            DocumentGetCommand,
            "doc_store_server.commands.retrieval_commands",
            "sync",
            "DocumentGetCommand.metadata",
            "DocumentGetCommand.get_schema",
        ),
        (
            "chapter_get",
            ChapterGetCommand,
            "doc_store_server.commands.retrieval_commands",
            "sync",
            "ChapterGetCommand.metadata",
            "ChapterGetCommand.get_schema",
        ),
        (
            "paragraph_get",
            ParagraphGetCommand,
            "doc_store_server.commands.retrieval_commands",
            "sync",
            "ParagraphGetCommand.metadata",
            "ParagraphGetCommand.get_schema",
        ),
        (
            "paragraph_get_by_number",
            ParagraphGetByNumberCommand,
            "doc_store_server.commands.retrieval_commands",
            "sync",
            "ParagraphGetByNumberCommand.metadata",
            "ParagraphGetByNumberCommand.get_schema",
        ),
        (
            "document_create",
            DocumentCreateCommand,
            "doc_store_server.commands.ingestion_commands",
            "queue",
            "DocumentCreateCommand.metadata",
            "DocumentCreateCommand.get_schema",
        ),
        (
            "document_update",
            DocumentUpdateCommand,
            "doc_store_server.commands.ingestion_commands",
            "queue",
            "DocumentUpdateCommand.metadata",
            "DocumentUpdateCommand.get_schema",
        ),
        (
            "document_chunk",
            DocumentChunkCommand,
            "doc_store_server.commands.ingestion_commands",
            "queue",
            "DocumentChunkCommand.metadata",
            "DocumentChunkCommand.get_schema",
        ),
        (
            "document_export",
            DocumentExportCommand,
            "doc_store_server.commands.document_export_command",
            "sync",
            "DocumentExportCommand.metadata",
            "DocumentExportCommand.get_schema",
        ),
        (
            "document_rebind",
            DocumentRebindCommand,
            "doc_store_server.commands.document_rebind_command",
            "sync",
            "DocumentRebindCommand.metadata",
            "DocumentRebindCommand.get_schema",
        ),
        (
            "processing_status",
            registration.ProcessingStatusCommand,
            "doc_store_server.commands.processing_status_command",
            "sync",
            "ProcessingStatusCommand.metadata",
            "ProcessingStatusCommand.get_schema",
        ),
        (
            "document_delete",
            DocumentDeleteCommand,
            "doc_store_server.commands.document_delete_command",
            "sync",
            "DocumentDeleteCommand.metadata",
            "DocumentDeleteCommand.get_schema",
        ),
        (
            "entity_create",
            EntityCreateCommand,
            "doc_store_server.commands.entity_lifecycle_commands",
            "sync",
            "EntityCreateCommand.metadata",
            "EntityCreateCommand.get_schema",
        ),
        (
            "entity_list",
            EntityListCommand,
            "doc_store_server.commands.entity_lifecycle_commands",
            "sync",
            "EntityListCommand.metadata",
            "EntityListCommand.get_schema",
        ),
        (
            "entity_get",
            EntityGetCommand,
            "doc_store_server.commands.entity_lifecycle_commands",
            "sync",
            "EntityGetCommand.metadata",
            "EntityGetCommand.get_schema",
        ),
        (
            "entity_update",
            EntityUpdateCommand,
            "doc_store_server.commands.entity_lifecycle_commands",
            "sync",
            "EntityUpdateCommand.metadata",
            "EntityUpdateCommand.get_schema",
        ),
        (
            "entity_soft_delete",
            EntitySoftDeleteCommand,
            "doc_store_server.commands.entity_lifecycle_commands",
            "sync",
            "EntitySoftDeleteCommand.metadata",
            "EntitySoftDeleteCommand.get_schema",
        ),
        (
            "entity_undelete",
            EntityUndeleteCommand,
            "doc_store_server.commands.entity_lifecycle_commands",
            "sync",
            "EntityUndeleteCommand.metadata",
            "EntityUndeleteCommand.get_schema",
        ),
        (
            "entity_hard_delete",
            EntityHardDeleteCommand,
            "doc_store_server.commands.entity_lifecycle_commands",
            "sync",
            "EntityHardDeleteCommand.metadata",
            "EntityHardDeleteCommand.get_schema",
        ),
        (
            "entity_references",
            EntityReferencesCommand,
            "doc_store_server.commands.entity_lifecycle_commands",
            "sync",
            "EntityReferencesCommand.metadata",
            "EntityReferencesCommand.get_schema",
        ),
        (
            "chunk_query_search",
            ChunkQuerySearchCommand,
            "doc_store_server.commands.chunk_query_search_command",
            "sync",
            "ChunkQuerySearchCommand.metadata",
            "ChunkQuerySearchCommand.get_schema",
        ),
    ]
    assert _manifest_rows() == expected


def test_registration_makes_one_explicit_custom_call_per_manifest_entry() -> None:
    registry = RecordingRegistry()

    registration.register_doc_store_commands(registry)

    _assert_exact_registration(registry.calls, registration.DOC_STORE_COMMAND_MANIFEST)
    assert all(command_type == "custom" for _, command_type in registry.calls)


def test_registration_contract_rejects_missing_unexpected_duplicate_and_inconsistent_calls() -> None:
    expected = [(entry.command_class, "custom") for entry in registration.DOC_STORE_COMMAND_MANIFEST]
    cases = {
        "missing": expected[:-1],
        "unexpected": [*expected, (object, "custom")],
        "duplicate": [*expected, expected[0]],
        "inconsistent": [(expected[0][0], "queue"), *expected[1:]],
    }

    for kind, calls in cases.items():
        with pytest.raises(AssertionError):
            _assert_exact_registration(calls, registration.DOC_STORE_COMMAND_MANIFEST)
        assert kind


def test_application_owns_the_single_custom_commands_hook() -> None:
    from mcp_proxy_adapter.commands.hooks import hooks

    assert hooks._custom_commands_hooks == [registration.register_doc_store_commands]


def test_every_manifest_command_has_complete_metadata_and_schema_contract() -> None:
    for entry in registration.DOC_STORE_COMMAND_MANIFEST:
        command = entry.command_class
        assert command.__module__ == entry.import_module
        assert command.name == entry.command_name
        assert entry.execution_mode == ("queue" if command.use_queue else "sync")
        assert entry.metadata_identity == f"{command.__name__}.metadata"
        assert entry.schema_identity == f"{command.__name__}.get_schema"

        metadata = command.metadata()
        assert set(metadata) == EXPECTED_METADATA_FIELDS
        assert metadata["name"] == command.name
        assert isinstance(metadata["parameters"], dict)
        assert all(
            isinstance(value, str) and value
            for value in metadata.values()
            if isinstance(value, str)
        )

        schema = command.get_schema()
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        assert set(schema["required"]) <= set(schema["properties"])
        assert schema["additionalProperties"] is False
        assert set(metadata["parameters"]) <= set(schema["properties"])
        for parameter_name, parameter in schema["properties"].items():
            assert "type" in parameter
            assert "description" in parameter
            assert parameter["type"]
            assert parameter["description"]


def test_command_set_is_not_discovered_by_scanning_or_dynamic_import() -> None:
    source_path = ROOT / "src/doc_store_server/commands/registration.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    source = source_path.read_text(encoding="utf-8")

    forbidden_tokens = ("pkgutil", "iter_modules", "importlib", "rglob", "glob(")
    assert not any(token in source for token in forbidden_tokens)
    register_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register"
    ]
    assert len(register_calls) == 1
    assert isinstance(register_calls[0].func.value, ast.Name)
    assert register_calls[0].func.value.id == "registry"


def test_import_has_no_global_command_registration_side_effect() -> None:
    code = """
from mcp_proxy_adapter.commands.command_registry import registry
before = set(registry._commands)
import doc_store_server.commands.registration
after = set(registry._commands)
assert after == before
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_manifest_entries_are_immutable_and_reject_identity_drift() -> None:
    entry = registration.DOC_STORE_COMMAND_MANIFEST[0]
    drifted = replace(entry, command_name="unexpected")
    assert drifted != entry
    assert entry.command_name == entry.command_class.name
