"""Focused contracts for semantic relation discovery command."""

from __future__ import annotations

import asyncio
from typing import Any

from doc_store_server.commands.semantic_relations_command import SemanticRelationsCommand


class Boundary:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "status": "ok",
            "scope": {"level": kwargs["level"]},
            "metric": kwargs["metric"],
            "threshold": kwargs["threshold"],
            "relation": kwargs["relation"],
            "model": "model",
            "dimension": 2,
            "groups": [],
            "pagination": {"limit": kwargs["limit"], "offset": kwargs["offset"], "total": 0},
            "diagnostics": {},
        }


def test_schema_and_metadata_describe_relation_contract() -> None:
    schema = SemanticRelationsCommand.get_schema()
    metadata = SemanticRelationsCommand.metadata()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["level"]["enum"] == ["document", "file", "paragraph", "chunk"]
    assert schema["properties"]["relation"]["enum"] == ["similar", "opposite"]
    assert metadata["name"] == "semantic_relations"
    assert "opposite" in metadata["detailed_description"].lower()
    assert metadata["parameters"] == schema["properties"]


def test_execute_delegates_to_injected_boundary_with_all_controls() -> None:
    boundary = Boundary()

    result = asyncio.run(
        SemanticRelationsCommand().execute(
            level="paragraph",
            relation="opposite",
            metric="cosine_similarity",
            threshold=0.2,
            project="7d",
            max_candidates=12,
            max_pairs=20,
            limit=3,
            offset=1,
            context={"semantic_relation_boundary": boundary},
        )
    )

    assert result.success is True
    assert result.data["scope"] == {"level": "paragraph"}
    assert boundary.calls == [
        {
            "level": "paragraph",
            "relation": "opposite",
            "metric": "cosine_similarity",
            "threshold": 0.2,
            "project": "7d",
            "document_id": None,
            "source_name": None,
            "seven_d_number": None,
            "include_deleted": False,
            "max_candidates": 12,
            "max_pairs": 20,
            "min_group_size": 2,
            "max_group_size": 20,
            "limit": 3,
            "offset": 1,
        }
    ]


def test_execute_reports_boundary_unavailable_and_invalid_params(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "doc_store_server.commands.semantic_relations_command.installed_semantic_relation_service",
        lambda: None,
    )
    missing = asyncio.run(SemanticRelationsCommand().execute())
    assert missing.to_dict()["error"]["data"]["code"] == "RELATION_BOUNDARY_UNAVAILABLE"

    class FailingBoundary:
        def search(self, **_: Any) -> dict[str, Any]:
            raise ValueError("bad threshold")

    invalid = asyncio.run(
        SemanticRelationsCommand().execute(context={"semantic_relation_boundary": FailingBoundary()})
    )
    assert invalid.to_dict()["error"]["data"]["code"] == "INVALID_PARAMS"
