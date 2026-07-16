"""Focused contract tests for the typed hierarchy retrieval commands."""

from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from doc_store_server.commands.retrieval_commands import (
    ChapterGetCommand,
    DocumentGetCommand,
    InvalidVersionError,
    ParagraphGetByNumberCommand,
    ParagraphGetCommand,
)
from mcp_proxy_adapter.core.errors import ValidationError


IDENTIFIER = UUID("550e8400-e29b-41d4-a716-446655440000")


@dataclass
class TypedDocument:
    title: str
    source_version: int

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {"title": self.title, "source_version": self.source_version}


class RecordingRetrievalBoundary:
    """Fake the canonical retrieval boundary, never transport or persistence."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result if result is not None else {"id": str(IDENTIFIER)}
        self.error = error
        self.calls: list[tuple[str, UUID, int | None]] = []

    async def _get(self, level: str, identifier: UUID, source_version: int | None) -> Any:
        self.calls.append((level, identifier, source_version))
        if self.error is not None:
            raise self.error
        return self.result

    async def get_document(self, document_id: UUID, source_version: int | None = None) -> Any:
        return await self._get("document", document_id, source_version)

    async def get_chapter(self, chapter_id: UUID, source_version: int | None = None) -> Any:
        return await self._get("chapter", chapter_id, source_version)

    async def get_paragraph(self, paragraph_id: UUID, source_version: int | None = None) -> Any:
        return await self._get("paragraph", paragraph_id, source_version)

    async def get_paragraph_by_number(
        self,
        document_id: UUID,
        paragraph_number: int,
        source_version: int | None = None,
    ) -> Any:
        self.calls.append(("paragraph_by_number", document_id, source_version))
        if self.error is not None:
            raise self.error
        return {
            "id": str(IDENTIFIER),
            "document_id": str(document_id),
            "paragraph_number": paragraph_number,
            "text": "second paragraph",
        }


def execute_validated(command_class: type[Any], boundary: RecordingRetrievalBoundary, **params: Any) -> Any:
    command = command_class()
    validated = command.validate_params(params)
    return asyncio.run(command.execute(**validated, context={"retrieval_boundary": boundary}))


@pytest.mark.parametrize(
    ("command_class", "identifier_field", "level"),
    [
        (DocumentGetCommand, "document_id", "document"),
        (ChapterGetCommand, "chapter_id", "chapter"),
        (ParagraphGetCommand, "paragraph_id", "paragraph"),
    ],
)
def test_valid_typed_identifier_delegates_only_to_requested_aggregate(
    command_class: type[Any], identifier_field: str, level: str
) -> None:
    boundary = RecordingRetrievalBoundary(result=TypedDocument("Guide", 3))

    result = execute_validated(
        command_class, boundary, **{identifier_field: str(IDENTIFIER), "source_version": 3}
    )

    assert result.success is True
    assert boundary.calls == [(level, IDENTIFIER, 3)]
    assert result.to_dict() == {
        "success": True,
        "data": {
            "entity": level,
            "identifier": str(IDENTIFIER),
            "source_version": 3,
            "value": {"title": "Guide", "source_version": 3},
        },
    }
    json.dumps(result.to_dict())


@pytest.mark.parametrize(
    ("command_class", "identifier_field"),
    [
        (DocumentGetCommand, "document_id"),
        (ChapterGetCommand, "chapter_id"),
        (ParagraphGetCommand, "paragraph_id"),
    ],
)
def test_omitted_source_version_is_forwarded_as_none(
    command_class: type[Any], identifier_field: str
) -> None:
    boundary = RecordingRetrievalBoundary(result={"version": 7})

    result = execute_validated(command_class, boundary, **{identifier_field: str(IDENTIFIER)})

    assert result.success is True
    assert boundary.calls == [(command_class.entity_name, IDENTIFIER, None)]
    assert result.data["source_version"] is None
    assert result.data["value"] == {"version": 7}


@pytest.mark.parametrize(
    ("command_class", "identifier_field"),
    [
        (DocumentGetCommand, "document_id"),
        (ChapterGetCommand, "chapter_id"),
        (ParagraphGetCommand, "paragraph_id"),
    ],
)
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (LookupError("missing"), "NOT_FOUND"),
        (InvalidVersionError("version 9 is not visible"), "INVALID_VERSION"),
    ],
)
def test_boundary_errors_have_stable_codes(
    command_class: type[Any], identifier_field: str, error: Exception, code: str
) -> None:
    boundary = RecordingRetrievalBoundary(error=error)

    result = execute_validated(command_class, boundary, **{identifier_field: str(IDENTIFIER)})

    assert result.success is False
    assert result.data == {}
    assert result.error == f"{code}: {error}"


@pytest.mark.parametrize(
    ("command_class", "identifier_field"),
    [
        (DocumentGetCommand, "document_id"),
        (ChapterGetCommand, "chapter_id"),
        (ParagraphGetCommand, "paragraph_id"),
    ],
)
def test_validation_rejects_missing_unknown_and_invalid_parameters(
    command_class: type[Any], identifier_field: str
) -> None:
    command = command_class()

    with pytest.raises(ValidationError):
        command.validate_params({})
    with pytest.raises(ValidationError):
        command.validate_params({identifier_field: str(IDENTIFIER), "unexpected": True})
    with pytest.raises(ValidationError):
        command.validate_params({identifier_field: str(uuid4()), "source_version": 0})
    with pytest.raises(ValidationError):
        command.validate_params({identifier_field: str(uuid4()), "source_version": True})
    with pytest.raises(ValidationError):
        command.validate_params({identifier_field: "not-a-uuid"})


@pytest.mark.parametrize("command_class", [DocumentGetCommand, ChapterGetCommand, ParagraphGetCommand])
def test_each_command_exposes_complete_live_schema_and_metadata(command_class: type[Any]) -> None:
    schema = command_class.get_schema()
    metadata = command_class.metadata()

    identifier_field = command_class.identifier_field
    assert schema["type"] == "object"
    assert schema["properties"] == command_class.schema_properties
    assert set(schema["properties"]) == {identifier_field, "source_version"}
    assert schema["required"] == [identifier_field]
    assert schema["additionalProperties"] is False
    assert set(metadata) == {
        "name", "version", "description", "category", "author", "email",
        "detailed_description", "parameters", "return_value", "usage_examples",
        "error_cases", "best_practices",
    }
    assert metadata["name"] == command_class.name
    assert metadata["parameters"] == command_class.parameter_docs
    assert metadata["return_value"] == command_class.return_contract
    assert {"NOT_FOUND", "INVALID_VERSION", "INTERNAL_ERROR"} <= metadata["error_cases"].keys()
    assert metadata["usage_examples"]
    assert metadata["best_practices"]


def test_paragraph_get_by_number_uses_document_and_one_based_number() -> None:
    boundary = RecordingRetrievalBoundary()

    result = execute_validated(
        ParagraphGetByNumberCommand,
        boundary,
        document_id=str(IDENTIFIER),
        paragraph_number=2,
        source_version=3,
    )

    assert result.success is True
    assert boundary.calls == [("paragraph_by_number", IDENTIFIER, 3)]
    assert result.data["document_id"] == str(IDENTIFIER)
    assert result.data["paragraph_number"] == 2
    assert result.data["text"] == "second paragraph"
    assert result.data["value"]["paragraph_number"] == 2


def test_paragraph_get_by_number_rejects_non_positive_number() -> None:
    command = ParagraphGetByNumberCommand()

    with pytest.raises(ValidationError):
        command.validate_params({"document_id": str(IDENTIFIER), "paragraph_number": 0})
    with pytest.raises(ValidationError):
        command.validate_params({"document_id": str(IDENTIFIER), "paragraph_number": True})


def test_paragraph_get_by_number_schema_documents_human_numbering() -> None:
    schema = ParagraphGetByNumberCommand.get_schema()
    metadata = ParagraphGetByNumberCommand.metadata()

    assert schema["required"] == ["document_id", "paragraph_number"]
    assert schema["properties"]["paragraph_number"]["minimum"] == 1
    assert metadata["name"] == "paragraph_get_by_number"
    assert "paragraph_number" in metadata["parameters"]
