"""Focused contract tests for the adapter-only query compiler."""

from __future__ import annotations

import inspect

import pytest
from chunk_metadata_adapter import ChunkQuery

import doc_store_server.query.compiler as compiler
from doc_store_server.query.compiler import (
    ExecutionMode,
    QueryContractError,
    QueryFieldError,
    QueryModeError,
    QueryPredicateError,
    compile_query,
)


def test_chunk_query_fields_are_normalized_and_bound_separately() -> None:
    plan = compile_query(
        ChunkQuery(
            project="doc-store",
            ordinal="7",
            quality_score="0.8",
            max_results="4",
        )
    )

    assert plan.mode is ExecutionMode.STRUCTURED
    assert plan.limit == 4
    assert plan.offset == 0
    assert [(item.column, item.operator, item.parameter) for item in plan.predicates.predicates] == [
        ("ordinal", "=", "p0"),
        ("project", "=", "p1"),
        ("quality_score", "=", "p2"),
    ]
    assert [item.value for item in plan.predicates.predicates] == ["7", "doc-store", "0.8"]
    assert plan.predicates.conjunction == "AND"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"unknown_field": "value"}, QueryFieldError),
        ({"project": {"$ne": "value"}}, QueryContractError),
        ({"tags": "not-a-sequence"}, QueryPredicateError),
        ({"filter_expr": "project = 'x' OR 1=1"}, QueryPredicateError),
        ({"max_results": 0}, QueryContractError),
        ({"max_results": -1}, QueryContractError),
        ({"offset": -1}, QueryContractError),
        ({"offset": 100001}, QueryContractError),
        ({"limit": 0}, QueryContractError),
        ({"limit": 1001}, QueryContractError),
        ({"limit": 5, "max_results": 6}, QueryContractError),
        ({"order_by": ["project"]}, QueryFieldError),
        ({"search_fields": ["body", "not-a-public-field"]}, QueryContractError),
    ],
)
def test_invalid_fields_operators_boolean_composition_and_paging_are_typed(
    payload: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        compile_query(payload)


def test_injection_like_values_remain_bound_data() -> None:
    value = "x' OR 1=1; DROP TABLE semantic_chunks; --"

    plan = compile_query({"project": value, "body": value})

    assert {item.column for item in plan.predicates.predicates} == {"project", "body"}
    assert {item.operator for item in plan.predicates.predicates} == {"="}
    assert {item.value for item in plan.predicates.predicates} == {value}
    assert all(item.parameter.startswith("p") for item in plan.predicates.predicates)
    assert "DROP TABLE" not in repr(plan.predicates).replace(value, "")


def test_structured_filter_only_payload_is_exact() -> None:
    plan = compile_query({"project": "doc-store", "max_results": 12, "offset": 3})

    assert plan == compiler.ExecutionPlan(
        mode=ExecutionMode.STRUCTURED,
        predicates=compiler.PredicateSet(
            (compiler.BoundPredicate("project", "=", "doc-store", "p0"),)
        ),
        search_fields=("body", "text", "summary", "title"),
        limit=12,
        offset=3,
    )


def test_limit_alias_compiles_to_execution_plan_limit() -> None:
    plan = compile_query({"project": "doc-store", "limit": "12", "offset": "4"})

    assert plan.mode is ExecutionMode.STRUCTURED
    assert plan.limit == 12
    assert plan.offset == 4


def test_block_meta_object_filter_compiles_to_jsonb_containment() -> None:
    plan = compile_query({"block_meta": {"scope": "client-upload"}, "max_results": 5})

    assert plan.mode is ExecutionMode.STRUCTURED
    assert plan.predicates.predicates == (
        compiler.BoundPredicate("block_meta", "@>", {"scope": "client-upload"}, "p0"),
    )


def test_full_text_only_payload_is_exact() -> None:
    plan = compile_query(
        {
            "search_query": "safe search",
            "search_fields": ["title", "body"],
            "bm25_k1": 1.4,
            "bm25_b": 0.7,
        }
    )

    assert plan.mode is ExecutionMode.FULL_TEXT
    assert plan.text == "safe search"
    assert plan.search_fields == ("title", "body")
    assert plan.embedding is None
    assert (plan.bm25_k1, plan.bm25_b) == (1.4, 0.7)
    assert plan.bm25_weight is None
    assert plan.semantic_weight is None


def test_semantic_only_payload_is_exact() -> None:
    plan = compile_query({"embedding": ["1", 2.5], "min_score": 0.25})

    assert plan.mode is ExecutionMode.SEMANTIC
    assert plan.embedding == (1.0, 2.5)
    assert plan.text is None
    assert plan.min_score == 0.25
    assert plan.bm25_k1 is None
    assert plan.bm25_weight is None


def test_hybrid_payload_carries_only_hybrid_controls() -> None:
    plan = compile_query(
        {
            "search_query": "hybrid",
            "embedding": [[1, 2], [3, 4]],
            "hybrid_search": True,
            "bm25_weight": 0.6,
            "semantic_weight": 0.4,
            "min_score": 0.1,
        }
    )

    assert plan.mode is ExecutionMode.HYBRID
    assert plan.embedding == ((1.0, 2.0), (3.0, 4.0))
    assert plan.text == "hybrid"
    assert (plan.bm25_weight, plan.semantic_weight, plan.min_score) == (0.6, 0.4, 0.1)
    assert (plan.bm25_k1, plan.bm25_b) == (1.2, 0.75)


def test_conflicting_hybrid_input_is_rejected() -> None:
    with pytest.raises(QueryModeError, match="requires both"):
        compile_query({"hybrid_search": True, "search_query": "text"})


def test_adapter_ast_is_request_input_only_and_compiler_is_io_free() -> None:
    source = inspect.getsource(compiler)

    with pytest.raises(QueryPredicateError, match="adapter-owned"):
        compile_query({"filter_expr": "project = 'request data'"})

    assert "QuerySpec" not in source
    assert "Lark" not in source
    assert "parser" not in source.lower()
    assert "sqlalchemy" not in source.lower()
    assert "Session" not in source
    assert not any(name.endswith(("Repository", "Storage", "Response")) for name in vars(compiler))
    assert not any(callable(value) and name in {"execute", "retrieve", "fetch"} for name, value in vars(compiler).items())
