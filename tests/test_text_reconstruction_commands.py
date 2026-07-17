"""Contracts for chapter/source text reconstruction commands."""

from __future__ import annotations

import asyncio
from typing import Any

from doc_store_server.commands.text_reconstruction_commands import (
    ChapterTextGetCommand,
    SourceFileReconstructCommand,
)


class FakeTextReconstruction:
    def assemble_chapter_text(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "entity": "chapter",
            "selector": kwargs,
            "text": "chapter text",
            "body_sha256": "a" * 64,
            "range_map": [{"chunk_id": "550e8400-e29b-41d4-a716-446655440005"}],
        }

    def reconstruct_source_file(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "entity": "source_file",
            "selector": kwargs,
            "text": "source text",
            "body_sha256": "b" * 64,
            "range_map": [{"chunk_id": "550e8400-e29b-41d4-a716-446655440005"}],
        }


def test_chapter_text_get_uses_boundary_and_selector() -> None:
    result = asyncio.run(
        ChapterTextGetCommand().execute(
            chapter_id="550e8400-e29b-41d4-a716-446655440003",
            include_context=True,
            context={"text_reconstruction_boundary": FakeTextReconstruction()},
        )
    )

    assert result.success is True
    assert result.data["entity"] == "chapter"
    assert result.data["text"] == "chapter text"
    assert result.data["selector"]["chapter_id"] == "550e8400-e29b-41d4-a716-446655440003"
    assert result.data["selector"]["include_context"] is True


def test_source_file_reconstruct_uses_boundary_and_document_selector() -> None:
    result = asyncio.run(
        SourceFileReconstructCommand().execute(
            document_id="550e8400-e29b-41d4-a716-446655440001",
            context={"text_reconstruction_boundary": FakeTextReconstruction()},
        )
    )

    assert result.success is True
    assert result.data["entity"] == "source_file"
    assert result.data["selector"]["document_id"] == "550e8400-e29b-41d4-a716-446655440001"


def test_reconstruction_commands_reject_empty_selector_and_invalid_metadata_key() -> None:
    missing = asyncio.run(
        SourceFileReconstructCommand().execute(
            context={"text_reconstruction_boundary": FakeTextReconstruction()}
        )
    )
    invalid = asyncio.run(
        ChapterTextGetCommand().execute(
            metadata_filters={"source-name'); drop table semantic_chunks; --": "x"},
            context={"text_reconstruction_boundary": FakeTextReconstruction()},
        )
    )

    assert missing.details["code"] == "INVALID_PARAMS"
    assert invalid.details["code"] == "INVALID_PARAMS"


def test_reconstruction_commands_report_unavailable_boundary() -> None:
    result = asyncio.run(
        ChapterTextGetCommand().execute(chapter_id="550e8400-e29b-41d4-a716-446655440003")
    )

    assert result.details["code"] == "RECONSTRUCTION_BOUNDARY_UNAVAILABLE"


def test_reconstruction_commands_map_not_found_to_stable_error() -> None:
    class MissingText(FakeTextReconstruction):
        def reconstruct_source_file(self, **kwargs: Any) -> dict[str, Any]:
            raise LookupError("no current chunk text found for source selector")

    result = asyncio.run(
        SourceFileReconstructCommand().execute(
            document_id="550e8400-e29b-41d4-a716-446655440001",
            context={"text_reconstruction_boundary": MissingText()},
        )
    )

    assert result.details["code"] == "NOT_FOUND"
