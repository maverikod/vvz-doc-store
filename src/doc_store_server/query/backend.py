"""Thin dispatch boundary for compiled chunk-query execution plans."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from chunk_metadata_adapter import ChunkQueryResponse, SearchResponseBuilder, SearchResult

from .compiler import ExecutionMode, ExecutionPlan


class BackendError(ValueError):
    """Base class for backend dispatch and branch-contract failures."""


class UnknownExecutionModeError(BackendError):
    """Raised when a plan does not select one of the supported branches."""


class BranchContractError(BackendError):
    """Raised when a branch returns something other than standard results."""


@dataclass(frozen=True, slots=True)
class QueryExecutionContext:
    """Call context shared unchanged by every branch owner."""

    session: Any
    limit: int | None = None
    offset: int = 0
    cursor: Any = None
    cancellation: Any = None
    timeout: Any = None


def _branch_name(mode: ExecutionMode) -> str:
    return mode.value


def _as_results(value: Any) -> tuple[SearchResult, ...]:
    if isinstance(value, ChunkQueryResponse):
        if not value.is_success:
            raise BranchContractError(value.error_message or "branch returned an error response")
        value = value.results
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Iterable):
        raise BranchContractError("branch must return SearchResult values")
    results = tuple(value)
    if any(type(result) is not SearchResult for result in results):
        raise BranchContractError("branch must return only standard SearchResult values")
    return results


def _response(
    results: Iterable[SearchResult],
    *,
    mode: str,
    limit: int | None,
    offset: int,
    cursor: Any,
    cancellation: Any,
    timeout: Any,
    continuation: Any,
) -> ChunkQueryResponse:
    builder = SearchResponseBuilder()
    for result in results:
        builder.add_result(result)
    metadata: dict[str, Any] = {
        "backend": "postgresql",
        "mode": mode,
        "limit": limit,
        "offset": offset,
        "cursor": cursor,
        "returned_count": len(builder.results),
        "continuation": continuation,
        "cancelled": cancellation,
        "timeout": timeout,
        "compatibility": "chunk-metadata-adapter",
    }
    return builder.set_metadata(metadata).build()


def _error_response(mode: str, error: BaseException) -> ChunkQueryResponse:
    """Build a standard adapter error response for a failed branch dispatch."""

    return SearchResponseBuilder().build_error(f"{mode} dispatch_error: {error}")


class QueryBackend:
    """Dispatch one compiled plan to exactly one injected execution owner."""

    def __init__(
        self,
        *,
        structured: Callable[..., Any],
        full_text: Callable[..., Any],
        semantic: Callable[..., Any],
        hybrid: Callable[..., Any],
    ) -> None:
        self._owners = {
            ExecutionMode.STRUCTURED: structured,
            ExecutionMode.FULL_TEXT: full_text,
            ExecutionMode.SEMANTIC: semantic,
            ExecutionMode.HYBRID: hybrid,
        }

    async def execute(
        self,
        plan: ExecutionPlan,
        *,
        session: Any,
        limit: int | None = None,
        offset: int = 0,
        cursor: Any = None,
        cancellation: Any = None,
        timeout: Any = None,
        continuation: Any = None,
    ) -> ChunkQueryResponse:
        if not isinstance(plan, ExecutionPlan):
            raise BranchContractError("backend accepts only compiled ExecutionPlan values")
        owner = self._owners.get(plan.mode)
        if owner is None:
            raise UnknownExecutionModeError(f"unknown execution mode: {plan.mode!r}")
        context = QueryExecutionContext(session, limit, offset, cursor, cancellation, timeout)
        try:
            result = owner(
                plan,
                session=context.session,
                limit=context.limit,
                offset=context.offset,
                cursor=context.cursor,
                cancellation=context.cancellation,
                timeout=context.timeout,
            )
            if inspect.isawaitable(result):
                result = await result
            results = _as_results(result)
        except (asyncio.CancelledError, TimeoutError, BranchContractError):
            raise
        except Exception as exc:
            return _error_response(_branch_name(plan.mode), exc)
        return _response(
            results,
            mode=_branch_name(plan.mode),
            limit=limit,
            offset=offset,
            cursor=cursor,
            cancellation=cancellation,
            timeout=timeout,
            continuation=continuation,
        )


async def dispatch_query(
    plan: ExecutionPlan, *, backend: QueryBackend, **context: Any
) -> ChunkQueryResponse:
    """Execute a compiled plan through an already configured backend."""

    return await backend.execute(plan, **context)


__all__ = [
    "BackendError",
    "BranchContractError",
    "QueryBackend",
    "QueryExecutionContext",
    "UnknownExecutionModeError",
    "dispatch_query",
]
