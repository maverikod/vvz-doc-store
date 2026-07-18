"""Focused contract tests for semantic chunk text version commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from doc_store_server.commands.chunk_version_commands import (
    ChunkVersionDeleteCommand,
    ChunkVersionListCommand,
    ChunkVersionSetCurrentCommand,
)
from doc_store_server.runtime.chunk_versions import (
    LAST_VERSION_DELETE_CODE,
    ChunkTextVersionError,
)


CHUNK_ID = "550e8400-e29b-41d4-a716-446655440000"


class FakeChunkVersionBoundary:
    def __init__(self, outcome: Any = None) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_versions(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_versions", kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome or {"chunk_id": CHUNK_ID, "items": []}

    def set_current(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("set_current", kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome or {"chunk_id": CHUNK_ID, "outcome": "set_current"}

    def delete_version(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_version", kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome or {"chunk_id": CHUNK_ID, "outcome": "deleted"}


def _error_code(result: Any) -> str:
    return result.to_dict()["error"]["data"]["code"]


def test_chunk_version_list_returns_paginated_stable_summaries() -> None:
    created_at = datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc)
    boundary = FakeChunkVersionBoundary(
        {
            "chunk_id": CHUNK_ID,
            "items": [
                {
                    "version_no": 1,
                    "text": "first version",
                    "created_at": created_at,
                    "is_current": False,
                    "char_count": 13,
                    "text_sha256": "checksum-1",
                    "block_meta": {"comment": "initial"},
                },
                {
                    "version_no": 2,
                    "preview": "second preview",
                    "created_at": "2026-07-18T13:00:00+00:00",
                    "current": True,
                    "char_count": 15,
                    "checksum": "checksum-2",
                    "comment": "edited",
                },
            ],
        }
    )

    result = asyncio.run(
        ChunkVersionListCommand().execute(
            chunk_id=CHUNK_ID,
            limit=1,
            offset=1,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert result.data == {
        "chunk_id": CHUNK_ID,
        "items": [
            {
                "version_no": 2,
                "preview": "second preview",
                "created_at": "2026-07-18T13:00:00+00:00",
                "current": True,
                "char_count": 15,
                "checksum": "checksum-2",
                "text_sha256": "checksum-2",
                "comment": "edited",
            }
        ],
        "total": 2,
        "offset": 1,
        "limit": 1,
    }
    assert boundary.calls == [("list_versions", {"chunk_id": CHUNK_ID})]


@pytest.mark.parametrize(
    ("command", "kwargs"),
    [
        (ChunkVersionListCommand(), {"chunk_id": "not-a-uuid"}),
        (ChunkVersionSetCurrentCommand(), {"chunk_id": "not-a-uuid", "version_no": 1}),
        (ChunkVersionDeleteCommand(), {"chunk_id": "not-a-uuid", "version_no": 1}),
    ],
)
def test_chunk_version_commands_validate_uuid4(command: Any, kwargs: dict[str, Any]) -> None:
    boundary = FakeChunkVersionBoundary()

    result = asyncio.run(command.execute(context={"chunk_version_boundary": boundary}, **kwargs))

    assert _error_code(result) == "INVALID_PARAMS"
    assert boundary.calls == []


@pytest.mark.parametrize("limit, offset", [(0, 0), (1001, 0), (1, -1), (1, 10_000_001)])
def test_chunk_version_list_rejects_pagination_bounds(limit: int, offset: int) -> None:
    boundary = FakeChunkVersionBoundary()

    result = asyncio.run(
        ChunkVersionListCommand().execute(
            chunk_id=CHUNK_ID,
            limit=limit,
            offset=offset,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert _error_code(result) == "INVALID_PARAMS"
    assert boundary.calls == []


@pytest.mark.parametrize("command", [ChunkVersionSetCurrentCommand(), ChunkVersionDeleteCommand()])
@pytest.mark.parametrize("version_no", [0, -1, True, "1"])
def test_chunk_version_mutations_reject_invalid_version_number(command: Any, version_no: Any) -> None:
    boundary = FakeChunkVersionBoundary()

    result = asyncio.run(
        command.execute(
            chunk_id=CHUNK_ID,
            version_no=version_no,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert _error_code(result) == "INVALID_PARAMS"
    assert boundary.calls == []


def test_chunk_version_set_current_routes_boundary_call() -> None:
    boundary = FakeChunkVersionBoundary({"chunk_id": CHUNK_ID, "outcome": "set_current", "version_no": 7})

    result = asyncio.run(
        ChunkVersionSetCurrentCommand().execute(
            chunk_id=CHUNK_ID,
            version_no=7,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert result.data["version_no"] == 7
    assert boundary.calls == [("set_current", {"chunk_id": CHUNK_ID, "version_no": 7})]


def test_chunk_version_delete_routes_boundary_call() -> None:
    boundary = FakeChunkVersionBoundary({"chunk_id": CHUNK_ID, "outcome": "deleted", "deleted_version_no": 3})

    result = asyncio.run(
        ChunkVersionDeleteCommand().execute(
            chunk_id=CHUNK_ID,
            version_no=3,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert result.data["deleted_version_no"] == 3
    assert boundary.calls == [("delete_version", {"chunk_id": CHUNK_ID, "version_no": 3})]


@pytest.mark.parametrize("command_name", ["set_current", "delete_version"])
def test_chunk_version_not_found_is_propagated(command_name: str) -> None:
    boundary = FakeChunkVersionBoundary(ChunkTextVersionError("VERSION_NOT_FOUND", "version missing"))
    command = ChunkVersionSetCurrentCommand() if command_name == "set_current" else ChunkVersionDeleteCommand()

    result = asyncio.run(
        command.execute(
            chunk_id=CHUNK_ID,
            version_no=2,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert _error_code(result) == "NOT_FOUND"
    assert "version missing" in result.to_dict()["error"]["message"]


def test_chunk_version_delete_last_is_stable_error() -> None:
    boundary = FakeChunkVersionBoundary(
        ChunkTextVersionError(LAST_VERSION_DELETE_CODE, "delete the chunk instead of deleting its last text version")
    )

    result = asyncio.run(
        ChunkVersionDeleteCommand().execute(
            chunk_id=CHUNK_ID,
            version_no=1,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert _error_code(result) == "LAST_VERSION_DELETE_FORBIDDEN"


@pytest.mark.parametrize(
    ("command", "kwargs"),
    [
        (ChunkVersionListCommand(), {"chunk_id": CHUNK_ID}),
        (ChunkVersionSetCurrentCommand(), {"chunk_id": CHUNK_ID, "version_no": 1}),
        (ChunkVersionDeleteCommand(), {"chunk_id": CHUNK_ID, "version_no": 1}),
    ],
)
def test_chunk_version_boundary_unavailable_maps_stably(command: Any, kwargs: dict[str, Any]) -> None:
    result = asyncio.run(command.execute(**kwargs, context={"chunk_version_boundary": object()}))

    assert _error_code(result) == "VERSION_BOUNDARY_UNAVAILABLE"


@pytest.mark.parametrize("command", [ChunkVersionListCommand(), ChunkVersionSetCurrentCommand(), ChunkVersionDeleteCommand()])
def test_chunk_version_internal_error_maps_stably(command: Any) -> None:
    boundary = FakeChunkVersionBoundary(RuntimeError("database unavailable"))
    kwargs = {"chunk_id": CHUNK_ID}
    if command.name != "chunk_version_list":
        kwargs["version_no"] = 1

    result = asyncio.run(command.execute(**kwargs, context={"chunk_version_boundary": boundary}))

    assert _error_code(result) == "INTERNAL_ERROR"
