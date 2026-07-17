"""Focused contracts for the typed, registry-backed info command."""

from __future__ import annotations

import ast
import asyncio
import copy
from importlib.metadata import distribution, version
from pathlib import Path
from typing import Any

import pytest

from doc_store_server.commands import registration
from doc_store_server.commands.info import (
    SECTION_NAMES,
    InfoCommand,
    build_info_document,
)


class RegistryFixture:
    """Small live-registry double returning the adapter help payload."""

    def __init__(self, commands: dict[str, dict[str, Any]]) -> None:
        self.commands = commands

    def get_all_commands_info(self) -> dict[str, Any]:
        return {"commands": copy.deepcopy(self.commands)}


def _command_entry(name: str, *, version: str = "9.4.1") -> dict[str, Any]:
    """Build one complete public command payload without a command catalog."""

    metadata = {
        "name": name,
        "version": version,
        "description": f"Description for {name}",
        "category": "fixture",
        "author": "Fixture Author",
        "email": "fixture@example.test",
        "detailed_description": f"Detailed description for {name}",
        "parameters": {"value": {"type": "string", "description": "Value."}},
        "parameters_docs": {
            "value": {"type": "string", "description": "Value."}
        },
        "return_value": {"description": f"Result for {name}."},
        "usage_examples": [{"value": "example"}],
        "error_cases": {"INVALID_VALUE": "Provide a valid value."},
        "best_practices": ["Use the command through the live adapter."],
        "summary": f"Description for {name}",
        "type": "custom",
    }
    return {
        "metadata": metadata,
        "schema": {
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "Value."}
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        "ai_metadata": {
            "name": name,
            "description": metadata["description"],
            "parameters": metadata["parameters"],
            "return_value": metadata["return_value"],
            "usage_examples": metadata["usage_examples"],
            "error_cases": metadata["error_cases"],
            "best_practices": metadata["best_practices"],
        },
    }


@pytest.fixture
def registry() -> RegistryFixture:
    """Provide generated command names so the test cannot hide a static catalog."""

    names = ("fixture_alpha", "fixture_omega")
    return RegistryFixture({name: _command_entry(name) for name in names})


def test_info_document_has_complete_deterministic_named_sections(
    registry: RegistryFixture,
) -> None:
    document = build_info_document(registry)

    assert tuple(section.name for section in document.sections) == SECTION_NAMES
    assert tuple(section.name for section in document.sections) == tuple(
        sorted(SECTION_NAMES, key=SECTION_NAMES.index)
    )
    assert all(section.title and section.content for section in document.sections)
    assert set(document.as_data()["sections"]) == set(SECTION_NAMES)

    for name in SECTION_NAMES:
        selected = document.as_data(name)
        assert tuple(selected["sections"]) == (name,)
        assert selected["selected_section"] == name
        assert selected["identity"] == document.as_data()["identity"]
        assert selected["command_reference"] == document.as_data()["command_reference"]


def test_info_identity_matches_independently_resolved_installed_build() -> None:
    installed_distribution = distribution("doc-store")
    independently_resolved_version = version(installed_distribution.metadata["Name"])
    document = build_info_document(RegistryFixture({}))

    assert independently_resolved_version == installed_distribution.version
    assert document.identity == {
        "package": installed_distribution.metadata["Name"],
        "package_version": independently_resolved_version,
        "build_version": independently_resolved_version,
        "command": "info",
    }
    assert InfoCommand.version == independently_resolved_version


def test_command_reference_is_exact_live_registry_projection_and_changes_with_fixture(
    registry: RegistryFixture,
) -> None:
    first = build_info_document(registry)
    expected = {name: registry.commands[name] for name in sorted(registry.commands)}
    assert first.command_reference == expected

    registry.commands["fixture_alpha"]["schema"]["properties"]["value"][
        "description"
    ] = "Changed live schema."
    registry.commands["fixture_zulu"] = _command_entry("fixture_zulu", version="10.0.0")
    second = build_info_document(registry)

    assert second.command_reference == {
        name: registry.commands[name] for name in sorted(registry.commands)
    }
    assert second.command_reference != first.command_reference
    assert "fixture_zulu" in second.command_reference
    assert "fixture_zulu" not in first.command_reference


def test_command_reference_preserves_complete_metadata_and_schema_fields(
    registry: RegistryFixture,
) -> None:
    reference = build_info_document(registry).command_reference

    for name, entry in reference.items():
        assert set(entry) == {"metadata", "schema", "ai_metadata"}
        metadata = entry["metadata"]
        assert {
            "name",
            "version",
            "description",
            "category",
            "author",
            "email",
            "detailed_description",
            "parameters",
            "parameters_docs",
            "return_value",
            "usage_examples",
            "error_cases",
            "best_practices",
            "summary",
            "type",
        } <= set(metadata)
        assert metadata["name"] == name
        assert metadata["parameters_docs"] == metadata["parameters"]
        assert entry["schema"]["properties"] == metadata["parameters"]
        assert entry["schema"]["required"] == ["value"]
        assert entry["ai_metadata"]["name"] == name


def test_info_command_reference_covers_every_manifest_command() -> None:
    commands = {
        entry.command_name: _command_entry(entry.command_name)
        for entry in registration.DOC_STORE_COMMAND_MANIFEST
    }
    data = build_info_document(RegistryFixture(commands)).as_data()

    assert set(data["command_reference"]) == {
        entry.command_name for entry in registration.DOC_STORE_COMMAND_MANIFEST
    }


def test_info_text_documents_bm25_and_owner_tree_commands(registry: RegistryFixture) -> None:
    serialized = str(build_info_document(registry).as_data())

    assert "bm25_tokens" in serialized
    assert "entity_type/entity_id" in serialized
    assert "arithmetic mean" in serialized
    assert "DOC_STORE_EMBEDDING_DIRECT_TEXT_MAX_CHARS" in serialized
    assert "semantic_refinement" in serialized
    assert "semantic_chunk_tokens" in serialized
    assert "entity_owner_tree" in serialized
    assert "entity_rebind_owner" in serialized
    assert "semantic_chunk_metadata_update" in serialized
    assert "chapter_text_get" in serialized
    assert "source_file_reconstruct" in serialized
    assert "range_map" in serialized
    assert "classification.provider" in serialized
    assert "review_status='machine'" in serialized


def test_unknown_section_has_documented_stable_error() -> None:
    result = asyncio.run(InfoCommand().execute(section="not_a_real_section"))

    assert result.success is False
    assert result.error == "UNKNOWN_SECTION: not_a_real_section"
    assert result.data == {"known_sections": list(SECTION_NAMES)}


def test_info_command_does_not_duplicate_fixture_command_catalog() -> None:
    source = Path(__file__).parents[1] / "src/doc_store_server/commands/info.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    source_text = source.read_text(encoding="utf-8")

    assert "fixture_alpha" not in source_text
    assert "fixture_omega" not in source_text
    assert not any(
        isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Constant) and key.value in {"fixture_alpha", "fixture_omega"}
            for key in node.keys
        )
        for node in ast.walk(tree)
    )
