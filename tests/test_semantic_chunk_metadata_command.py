"""Contracts for public SemanticChunk metadata updates."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import pytest

from doc_store_server.commands.semantic_chunk_metadata_command import (
    SemanticChunkMetadataUpdateCommand,
)
from doc_store_server.runtime.semantic_chunk_metadata import _validated_updates


CHUNK_ID = "550e8400-e29b-41d4-a716-446655440000"


class Boundary:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def update_metadata(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        return {
            "outcome": "updated",
            "requested": 1,
            "matched": 1,
            "updated": 1,
            "items": [{"chunk_id": CHUNK_ID, "updated_fields": ["category"]}],
            "dry_run": False,
        }


def test_semantic_chunk_metadata_update_metadata_paradigm() -> None:
    metadata = SemanticChunkMetadataUpdateCommand.metadata()
    schema = SemanticChunkMetadataUpdateCommand.get_schema()

    assert {
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
    } == set(metadata)
    assert metadata["name"] == "semantic_chunk_metadata_update"
    assert "weak/local open-source classifiers" in metadata["detailed_description"]
    assert schema["additionalProperties"] is False
    assert "classification" in schema["properties"]["updates"]["properties"]


def test_semantic_chunk_metadata_update_delegates_uuid4_checked_request() -> None:
    boundary = Boundary()
    result = asyncio.run(
        SemanticChunkMetadataUpdateCommand().execute(
            chunk_id=CHUNK_ID,
            updates={
                "category": "theory",
                "classification": {
                    "provider": "local",
                    "model": "small",
                    "confidence": 0.7,
                },
            },
            context={"semantic_chunk_metadata_boundary": boundary},
        )
    )

    assert result.success is True
    assert result.data["updated"] == 1
    assert boundary.calls[0]["chunk_id"] == CHUNK_ID
    assert boundary.calls[0]["updates"]["classification"]["model"] == "small"


def test_semantic_chunk_metadata_update_rejects_invalid_uuid_before_boundary() -> None:
    boundary = Boundary()
    result = asyncio.run(
        SemanticChunkMetadataUpdateCommand().execute(
            chunk_id="not-a-uuid",
            updates={"category": "theory"},
            context={"semantic_chunk_metadata_boundary": boundary},
        )
    )

    assert result.error
    assert result.details["code"] == "INVALID_PARAMS"
    assert boundary.calls == []


def test_semantic_chunk_metadata_update_accepts_batch_items() -> None:
    boundary = Boundary()
    result = asyncio.run(
        SemanticChunkMetadataUpdateCommand().execute(
            items=(
                {
                    "chunk_id": CHUNK_ID,
                    "updates": {
                        "language": "ru",
                        "classification": {
                            "provider": "local",
                            "model": "weak",
                            "confidence": 0.42,
                        },
                    },
                },
            ),
            context={"semantic_chunk_metadata_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls[0]["items"][0]["chunk_id"] == CHUNK_ID
    assert boundary.calls[0]["items"][0]["updates"]["language"] == "ru"


def test_semantic_chunk_metadata_update_validates_filter_uuid4_before_boundary() -> None:
    boundary = Boundary()
    result = asyncio.run(
        SemanticChunkMetadataUpdateCommand().execute(
            filters={"document_id": "550e8400-e29b-11d4-a716-446655440000"},
            updates={"category": "theory"},
            context={"semantic_chunk_metadata_boundary": boundary},
        )
    )

    assert result.error
    assert result.details["code"] == "INVALID_PARAMS"
    assert boundary.calls == []


def test_semantic_chunk_metadata_update_rejects_malformed_batch_item() -> None:
    boundary = Boundary()
    result = asyncio.run(
        SemanticChunkMetadataUpdateCommand().execute(
            items=("not-an-object",),
            context={"semantic_chunk_metadata_boundary": boundary},
        )
    )

    assert result.error
    assert result.details["code"] == "INVALID_PARAMS"
    assert boundary.calls == []


def test_semantic_chunk_metadata_validation_defaults_and_provenance() -> None:
    payload = _validated_updates(
        {
            "category": "",
            "language": None,
            "classification": {
                "provider": "local",
                "model": "tiny-classifier",
                "confidence": "0.51",
            },
        }
    )

    assert payload["category"] == "uncategorized"
    assert payload["language"] == "UNKNOWN"
    assert payload["classification"] == {
        "provider": "local",
        "model": "tiny-classifier",
        "confidence": 0.51,
        "review_status": "machine",
    }


def test_semantic_chunk_metadata_validation_rejects_forbidden_fields() -> None:
    with pytest.raises(ValueError, match="forbidden SemanticChunk metadata fields"):
        _validated_updates({"bm25_tokens": ["alpha"]})

    with pytest.raises(ValueError, match="forbidden SemanticChunk metadata fields"):
        _validated_updates({"quality_score": 0.9})
