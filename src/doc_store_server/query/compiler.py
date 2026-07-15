"""Pure compiler for the public chunk-metadata-adapter query contract.

This module deliberately stops at an immutable execution description.  It does
not import storage models, parse adapter expressions, or execute a query.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from chunk_metadata_adapter import ChunkQuery, SemanticChunk
from pydantic import ValidationError


class QueryCompilationError(ValueError):
    """Base class for typed query compilation failures."""


class QueryContractError(QueryCompilationError):
    """The public query payload is not a valid ChunkQuery contract."""


class QueryFieldError(QueryContractError):
    """A field is unknown or is not valid for the requested operation."""


class QueryModeError(QueryContractError):
    """Search inputs select an invalid or ambiguous execution mode."""


class QueryPredicateError(QueryContractError):
    """A metadata predicate cannot be represented by the typed allowlist."""


class ExecutionMode(str, Enum):
    STRUCTURED = "structured"
    FULL_TEXT = "full_text"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class BoundPredicate:
    """One allowlisted SQL predicate and its separately bound value."""

    column: str
    operator: str
    value: Any
    parameter: str


@dataclass(frozen=True, slots=True)
class PredicateSet:
    """Immutable boolean composition of bound predicates."""

    predicates: tuple[BoundPredicate, ...] = ()
    conjunction: str = "AND"


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """All inputs a downstream executor needs, and nothing that executes it."""

    mode: ExecutionMode
    predicates: PredicateSet
    text: str | None = None
    search_fields: tuple[str, ...] = ()
    embedding: tuple[float, ...] | tuple[tuple[float, ...], ...] | None = None
    bm25_k1: float | None = None
    bm25_b: float | None = None
    bm25_weight: float | None = None
    semantic_weight: float | None = None
    min_score: float | None = None
    limit: int | None = None
    offset: int = 0
    order_by: tuple[str, ...] = ()

    @property
    def search_query(self) -> str | None:
        return self.text


_QUERY_FIELDS = frozenset(ChunkQuery.model_fields)
_CHUNK_FIELDS = frozenset(SemanticChunk.model_fields)
_CONTROL_FIELDS = frozenset(
    {
        "search_query",
        "search_fields",
        "bm25_k1",
        "bm25_b",
        "hybrid_search",
        "bm25_weight",
        "semantic_weight",
        "min_score",
        "max_results",
        "filter_expr",
    }
)
_FILTER_FIELDS = frozenset(_CHUNK_FIELDS - _CONTROL_FIELDS - {"embedding"})
_TEXT_FIELDS = frozenset({"body", "text", "summary", "title"})
_ORDER_FIELDS = frozenset(_FILTER_FIELDS)


def _mapping_for_query(query: ChunkQuery | Mapping[str, Any]) -> tuple[Mapping[str, Any], frozenset[str]]:
    if isinstance(query, ChunkQuery):
        values = query.model_dump(exclude_unset=False)
        extras = getattr(query, "model_extra", None) or {}
        if extras:
            values.update(extras)
        return values, frozenset(query.model_fields_set) | frozenset(extras)
    if not isinstance(query, Mapping):
        raise QueryContractError("query must be a ChunkQuery or mapping")
    return query, frozenset(query)


def _validate_public_fields(values: Mapping[str, Any]) -> None:
    unknown = sorted(set(values) - _QUERY_FIELDS)
    if unknown:
        raise QueryFieldError(f"unknown ChunkQuery fields: {', '.join(unknown)}")


def _normalise_embedding(value: Any) -> tuple[float, ...] | tuple[tuple[float, ...], ...] | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise QueryModeError("embedding must be a sequence of numbers")
    if value and isinstance(value[0], Sequence) and not isinstance(value[0], (str, bytes)):
        return tuple(tuple(float(item) for item in row) for row in value)
    return tuple(float(item) for item in value)


def _compile_predicates(values: Mapping[str, Any], provided: frozenset[str]) -> PredicateSet:
    predicates: list[BoundPredicate] = []
    for field in sorted(_FILTER_FIELDS):
        if field not in provided:
            continue
        value = values.get(field)
        if value is None:
            continue
        if field not in _ORDER_FIELDS:
            raise QueryPredicateError(f"field is not filterable: {field}")
        if isinstance(value, Mapping):
            raise QueryPredicateError(f"boolean/operator AST is not accepted for {field}")
        if field in {"tags", "links"}:
            if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
                raise QueryPredicateError(f"{field} requires a sequence of strings")
            operator = "@>"
            bound = tuple(value)
        else:
            operator = "="
            bound = value.value if isinstance(value, Enum) else value
        predicates.append(BoundPredicate(field, operator, bound, f"p{len(predicates)}"))
    return PredicateSet(tuple(predicates))


def compile_query(query: ChunkQuery | Mapping[str, Any]) -> ExecutionPlan:
    """Validate, normalize, and compile a query without retrieval or I/O."""

    values, provided = _mapping_for_query(query)
    _validate_public_fields(values)
    if values.get("filter_expr") is not None:
        raise QueryPredicateError("filter_expr is adapter-owned and cannot be executed")
    try:
        normalized = ChunkQuery.model_validate(dict(values))
    except ValidationError as exc:
        raise QueryContractError(str(exc)) from exc
    values = normalized.model_dump()

    text = values.get("search_query")
    embedding = _normalise_embedding(values.get("embedding"))
    hybrid = bool(values.get("hybrid_search"))
    has_text = isinstance(text, str) and bool(text.strip())
    has_embedding = embedding is not None
    if hybrid and not (has_text and has_embedding):
        raise QueryModeError("hybrid_search requires both search_query and embedding")
    if has_text and has_embedding:
        mode = ExecutionMode.HYBRID
    elif has_text:
        mode = ExecutionMode.FULL_TEXT
    elif has_embedding:
        mode = ExecutionMode.SEMANTIC
    else:
        mode = ExecutionMode.STRUCTURED

    fields = tuple(values.get("search_fields") or ())
    if any(field not in _TEXT_FIELDS for field in fields):
        raise QueryFieldError("search_fields may contain only body, text, summary, or title")
    return ExecutionPlan(
        mode=mode,
        predicates=_compile_predicates(values, provided),
        text=text if has_text else None,
        search_fields=fields,
        embedding=embedding,
        bm25_k1=values.get("bm25_k1") if mode in (ExecutionMode.FULL_TEXT, ExecutionMode.HYBRID) else None,
        bm25_b=values.get("bm25_b") if mode in (ExecutionMode.FULL_TEXT, ExecutionMode.HYBRID) else None,
        bm25_weight=values.get("bm25_weight") if mode is ExecutionMode.HYBRID else None,
        semantic_weight=values.get("semantic_weight") if mode is ExecutionMode.HYBRID else None,
        min_score=values.get("min_score") if mode is not ExecutionMode.STRUCTURED else None,
        limit=values.get("max_results"),
    )


compile_chunk_query = compile_query
compile = compile_query

__all__ = [
    "BoundPredicate",
    "ExecutionMode",
    "ExecutionPlan",
    "PredicateSet",
    "QueryCompilationError",
    "QueryContractError",
    "QueryFieldError",
    "QueryModeError",
    "QueryPredicateError",
    "compile",
    "compile_chunk_query",
    "compile_query",
]
