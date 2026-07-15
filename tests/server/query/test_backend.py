"""Focused contract tests for the compiled-plan query dispatcher."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from typing import Any

import pytest
from chunk_metadata_adapter import ChunkQueryResponse, SearchResponseBuilder, SearchResult, SemanticChunk

import doc_store_server.query.backend as backend_module
from doc_store_server.query.backend import (
    BranchContractError,
    QueryBackend,
    UnknownExecutionModeError,
    dispatch_query,
)
from doc_store_server.query.compiler import ExecutionMode, compile_query


CHUNK_ID = "11111111-1111-4111-8111-111111111111"


def _result() -> SearchResult:
    chunk = SemanticChunk(
        uuid=CHUNK_ID,
        type="DocBlock",
        body="compiled branch result",
        text="compiled branch result",
        block_meta={"source": "fixture"},
    )
    return SearchResult(chunk_id=CHUNK_ID, chunk=chunk, rank=1)


def _plans() -> list[tuple[ExecutionMode, Any]]:
    return [
        (ExecutionMode.STRUCTURED, compile_query({"project": "doc-store"})),
        (ExecutionMode.FULL_TEXT, compile_query({"search_query": "needle"})),
        (ExecutionMode.SEMANTIC, compile_query({"embedding": [1, 2, 3]})),
        (
            ExecutionMode.HYBRID,
            compile_query(
                {"search_query": "needle", "embedding": [1, 2, 3], "hybrid_search": True}
            ),
        ),
    ]


@pytest.mark.parametrize("mode,plan", _plans())
def test_each_compiled_mode_dispatches_once_with_context_unchanged(
    mode: ExecutionMode, plan: Any
) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    session = object()
    cancellation = object()
    timeout = object()
    continuation = {"next": "cursor-2"}

    async def owner(received_plan: Any, **context: Any) -> list[SearchResult]:
        calls.append((received_plan, context))
        return [_result()]

    owners = {item: owner for item, _ in _plans()}
    backend = QueryBackend(**{
        "structured": owners[ExecutionMode.STRUCTURED],
        "full_text": owners[ExecutionMode.FULL_TEXT],
        "semantic": owners[ExecutionMode.SEMANTIC],
        "hybrid": owners[ExecutionMode.HYBRID],
    })

    response = asyncio.run(
        backend.execute(
            plan,
            session=session,
            limit=7,
            offset=3,
            cursor="cursor-1",
            cancellation=cancellation,
            timeout=timeout,
            continuation=continuation,
        )
    )

    assert len(calls) == 1
    received_plan, context = calls[0]
    assert received_plan is plan
    assert context == {
        "session": session,
        "limit": 7,
        "offset": 3,
        "cursor": "cursor-1",
        "cancellation": cancellation,
        "timeout": timeout,
    }
    assert type(response) is ChunkQueryResponse
    assert response.results == [_result()]
    assert response.metadata == {
        "backend": "postgresql",
        "mode": mode.value,
        "limit": 7,
        "offset": 3,
        "cursor": "cursor-1",
        "returned_count": 1,
        "continuation": continuation,
        "cancelled": cancellation,
        "timeout": timeout,
        "compatibility": "chunk-metadata-adapter",
    }


def test_dispatch_query_forwards_to_the_single_backend_owner() -> None:
    calls: list[Any] = []
    plan = compile_query({"project": "doc-store"})

    def structured(received_plan: Any, **context: Any) -> tuple[SearchResult, ...]:
        calls.append((received_plan, context))
        return (_result(),)

    backend = QueryBackend(
        structured=structured,
        full_text=lambda *_args, **_kwargs: (),
        semantic=lambda *_args, **_kwargs: (),
        hybrid=lambda *_args, **_kwargs: (),
    )
    response = asyncio.run(dispatch_query(plan, backend=backend, session="session"))

    assert response.is_success
    assert len(calls) == 1
    assert calls[0][0] is plan


@pytest.mark.parametrize("value", [None, "not-results", object(), [object()]])
def test_branch_contract_violations_are_rejected(value: Any) -> None:
    plan = compile_query({"project": "doc-store"})
    backend = QueryBackend(
        structured=lambda *_args, **_kwargs: value,
        full_text=lambda *_args, **_kwargs: (),
        semantic=lambda *_args, **_kwargs: (),
        hybrid=lambda *_args, **_kwargs: (),
    )

    with pytest.raises(BranchContractError, match="SearchResult"):
        asyncio.run(backend.execute(plan, session=object()))


def test_branch_error_response_is_a_contract_violation() -> None:
    plan = compile_query({"project": "doc-store"})
    error = SearchResponseBuilder().build_error("branch failed")
    backend = QueryBackend(
        structured=lambda *_args, **_kwargs: error,
        full_text=lambda *_args, **_kwargs: (),
        semantic=lambda *_args, **_kwargs: (),
        hybrid=lambda *_args, **_kwargs: (),
    )

    with pytest.raises(BranchContractError, match="branch failed"):
        asyncio.run(backend.execute(plan, session=object()))


def test_unexpected_branch_exception_normalizes_to_standard_error_response() -> None:
    plan = compile_query({"project": "doc-store"})
    backend = QueryBackend(
        structured=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        full_text=lambda *_args, **_kwargs: (),
        semantic=lambda *_args, **_kwargs: (),
        hybrid=lambda *_args, **_kwargs: (),
    )

    response = asyncio.run(backend.execute(plan, session=object()))

    assert type(response) is ChunkQueryResponse
    assert not response.is_success
    assert response.error_message == "structured dispatch_error: database unavailable"


def test_unknown_modes_and_non_compiled_inputs_fail_before_dispatch() -> None:
    backend = QueryBackend(
        structured=lambda *_args, **_kwargs: pytest.fail("must not dispatch"),
        full_text=lambda *_args, **_kwargs: pytest.fail("must not dispatch"),
        semantic=lambda *_args, **_kwargs: pytest.fail("must not dispatch"),
        hybrid=lambda *_args, **_kwargs: pytest.fail("must not dispatch"),
    )
    plan = compile_query({"project": "doc-store"})

    with pytest.raises(UnknownExecutionModeError, match="unknown execution mode"):
        asyncio.run(backend.execute(replace(plan, mode="legacy"), session=object()))
    with pytest.raises(BranchContractError, match="compiled ExecutionPlan"):
        asyncio.run(backend.execute(object(), session=object()))  # type: ignore[arg-type]


def test_dispatcher_does_not_duplicate_compilation_or_branch_algorithms() -> None:
    source = inspect.getsource(backend_module)

    assert "ChunkQuery(" not in source
    for forbidden in (
        "compile_query",
        "compile_chunk_query",
        "sqlalchemy",
        "SELECT ",
        "pgvector",
        "EmbeddingClient",
        "embedding",
        "fuse_search_results",
        "HybridSearchHelper",
        "Provider",
        "BaseModel",
    ):
        assert forbidden not in source
