"""Focused contract tests for the canonical chunk-query command boundary."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest
from chunk_metadata_adapter import ChunkQuery, ChunkQueryResponse, SearchResult, SemanticChunk

from doc_store_server.commands.chunk_query_search_command import ChunkQuerySearchCommand


COMMAND_SOURCE = Path(__file__).parents[1] / "src/doc_store_server/commands/chunk_query_search_command.py"


def _chunk(chunk_id: str = "11111111-1111-4111-8111-111111111111") -> SemanticChunk:
    return SemanticChunk(
        uuid=chunk_id,
        type="DocBlock",
        body="canonical body",
        text="normalized text",
        project="doc-store",
        block_meta={"provenance": "fixture"},
    )


def _result(chunk_id: str = "11111111-1111-4111-8111-111111111111") -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        chunk=_chunk(chunk_id),
        bm25_score=0.8,
        semantic_score=0.7,
        hybrid_score=0.75,
        rank=1,
        matched_fields=["body", "title"],
        highlights={"body": ["canonical body"]},
        search_metadata={"provenance": "fixture", "diagnostics": []},
    )


@pytest.fixture
def command() -> ChunkQuerySearchCommand:
    return ChunkQuerySearchCommand()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({"project": "doc-store"}, {"project": "doc-store"}),
        ({"search_query": "canonical", "search_fields": ["body", "title"]}, {"search_query": "canonical"}),
        ({"embedding": [0.1, 0.2, 0.3]}, {"embedding": [0.1, 0.2, 0.3]}),
        (
            {"search_query": "canonical", "embedding": [0.1, 0.2, 0.3], "hybrid_search": True},
            {"hybrid_search": True},
        ),
    ],
)
def test_validate_params_accepts_each_canonical_search_mode(
    command: ChunkQuerySearchCommand, query: dict[str, Any], expected: dict[str, Any]
) -> None:
    validated = command.validate_params({"query": query})

    assert type(validated["query"]) is ChunkQuery
    for field, value in expected.items():
        assert getattr(validated["query"], field) == value


def test_validate_params_preserves_typed_filters_limits_and_semantic_threshold(
    command: ChunkQuerySearchCommand,
) -> None:
    query = command.validate_params(
        {
            "query": {
                "project": "doc-store",
                "type": "DocBlock",
                "language": "en",
                "year": 2026,
                "is_public": True,
                "tags": ["canonical", "search"],
                "min_score": 0.82,
                "limit": 7,
                "offset": 14,
            }
        }
    )["query"]

    assert query.project == "doc-store"
    assert query.type == "DocBlock"
    assert query.language == "en"
    assert query.year == 2026
    assert query.is_public is True
    assert query.tags == ["canonical", "search"]
    assert query.min_score == pytest.approx(0.82)
    assert query.max_results == 100
    assert query.model_extra == {"limit": 7, "offset": 14}


def test_validate_params_keeps_semantic_refinement_outside_chunk_query(
    command: ChunkQuerySearchCommand,
) -> None:
    validated = command.validate_params(
        {
            "query": {"search_query": "semantic", "hybrid_search": True},
            "semantic_refinement": {"enabled": True, "threshold": 0.4, "candidate_limit": 12, "result_limit": 5},
        }
    )

    assert type(validated["query"]) is ChunkQuery
    assert validated["semantic_refinement"]["threshold"] == pytest.approx(0.4)
    assert "semantic_refinement" not in validated["query"].model_dump()


def test_validate_params_rejects_unknown_fields_and_legacy_lark_filter_text(
    command: ChunkQuerySearchCommand,
) -> None:
    with pytest.raises(Exception, match="unknown ChunkQuery fields"):
        command.validate_params({"query": {"project": "doc-store", "unknown": "value"}})

    with pytest.raises(Exception, match="legacy|filter_expr|canonical"):
        command.validate_params({"query": {"filter_expr": "title == 'legacy'"}})


def test_execute_delegates_exactly_once_and_preserves_response_payload(
    command: ChunkQuerySearchCommand,
) -> None:
    query = ChunkQuery(search_query="canonical", max_results=3, min_score=0.4)
    response = ChunkQueryResponse(
        {
            "status": "success",
            "data": {
                "results": [_result().to_dict()],
                "provenance": {"source": "G-007"},
                "diagnostics": {"took_ms": 4},
            },
        }
    )
    calls: list[tuple[ChunkQuery, dict[str, Any]]] = []

    def orchestrator(received: ChunkQuery, **context: Any) -> ChunkQueryResponse:
        calls.append((received, context))
        return response

    result = asyncio.run(
        command.execute(query=query, context={"search_orchestrator": orchestrator, "trace_id": "t-1"})
    )

    assert result.success is True
    assert result.data == response.to_dict()
    assert len(calls) == 1
    assert calls[0][0] is query
    assert calls[0][1] == {"trace_id": "t-1"}


def test_execute_supports_object_orchestrator_and_async_response(command: ChunkQuerySearchCommand) -> None:
    expected = [_result("22222222-2222-4222-8222-222222222222")]

    class Orchestrator:
        def __init__(self) -> None:
            self.calls: list[tuple[ChunkQuery, dict[str, Any]]] = []

        async def execute(self, query: ChunkQuery, **context: Any) -> list[SearchResult]:
            self.calls.append((query, context))
            return expected

    owner = Orchestrator()
    result = asyncio.run(command.execute(query=ChunkQuery(project="doc-store"), context={"search_orchestrator": owner}))

    assert result.success is True
    assert result.data == {"status": "success", "data": {"results": [expected[0].to_dict()]}}
    assert len(owner.calls) == 1


def test_execute_passes_semantic_refinement_as_command_context(command: ChunkQuerySearchCommand) -> None:
    calls: list[dict[str, Any]] = []

    def orchestrator(_: ChunkQuery, **context: Any) -> dict[str, Any]:
        calls.append(context)
        return {"status": "success", "data": {"results": []}}

    result = asyncio.run(
        command.execute(
            query=ChunkQuery(search_query="semantic"),
            semantic_refinement={"enabled": True, "threshold": 0.5},
            context={"search_orchestrator": orchestrator},
        )
    )

    assert result.success is True
    assert calls == [{"semantic_refinement": {"enabled": True, "threshold": 0.5}}]


def test_execute_reports_missing_and_failed_orchestrator_with_stable_errors(
    command: ChunkQuerySearchCommand,
) -> None:
    missing = asyncio.run(command.execute(query=ChunkQuery(project="doc-store")))
    assert missing.error.startswith("ORCHESTRATOR_UNAVAILABLE:")
    assert missing.details == {"remediation": "Provide context.search_orchestrator."}

    def failing(_: ChunkQuery) -> None:
        raise RuntimeError("fixture failure")

    failed = asyncio.run(command.execute(query=ChunkQuery(project="doc-store"), context={"search_orchestrator": failing}))
    assert failed.error == "SEARCH_EXECUTION_FAILED: fixture failure"
    assert failed.details == {"remediation": "Inspect G-007 diagnostics and retry the canonical request."}


def test_schema_and_metadata_are_complete_live_contracts() -> None:
    schema = ChunkQuerySearchCommand.get_schema()
    metadata = ChunkQuerySearchCommand.metadata()

    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["query"]["additionalProperties"] is False
    assert schema["properties"]["semantic_refinement"]["additionalProperties"] is False
    assert set(schema["properties"]["query"]["properties"]) == set(ChunkQuery.model_fields) | {"limit", "offset"}
    assert "offset" in metadata["parameters"]["query"]["properties"]
    assert "semantic_refinement" in metadata["parameters"]
    assert metadata["name"] == "chunk_query_search"
    assert metadata["parameters"] == schema["properties"]
    assert metadata["usage_examples"]
    assert metadata["error_cases"]
    assert metadata["best_practices"]
    assert "G-007" in metadata["detailed_description"]


def test_command_source_has_no_legacy_language_or_direct_backend_mechanisms() -> None:
    source = COMMAND_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(COMMAND_SOURCE))
    imported_modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported_modules.isdisjoint({"sqlalchemy", "asyncpg", "pgvector", "lark"})
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called_names.isdisjoint({"execute_sql", "search_full_text", "search_pgvector", "fuse_results"})
