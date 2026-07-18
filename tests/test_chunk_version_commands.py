"""Focused contract tests for semantic chunk text version commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from doc_store_server.commands.chunk_version_commands import (
    ChunkHistoryCommand,
    ChunkVersionAddCommand,
    ChunkVersionDeleteCommand,
    ChunkVersionDiffCommand,
    ChunkVersionGetCommand,
    ChunkVersionListCommand,
    ChunkVersionRestoreCommand,
    ChunkVersionRetireCommand,
    ChunkVersionSetCurrentCommand,
    ChunkVersionUpdateCommand,
)
from doc_store_server.runtime.chunk_versions import (
    CURRENT_VERSION_MISMATCH_CODE,
    CURRENT_VERSION_RETIRE_CODE,
    LAST_VERSION_DELETE_CODE,
    ChunkTextVersionError,
)


CHUNK_ID = "550e8400-e29b-41d4-a716-446655440000"
OPERATION_ID = "550e8400-e29b-41d4-a716-446655440001"


class FakeChunkVersionBoundary:
    def __init__(self, outcome: Any = None) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _return(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome or {"chunk_id": CHUNK_ID, "items": []}

    def list_versions(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("list_versions", kwargs)

    def history(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("history", kwargs)

    def get_version(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("get_version", kwargs)

    def append_version(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("append_version", kwargs)

    def update_text(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("update_text", kwargs)

    def set_current(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("set_current", kwargs)

    def restore_version(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("restore_version", kwargs)

    def retire_version(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("retire_version", kwargs)

    def delete_version(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("delete_version", kwargs)

    def diff_versions(self, **kwargs: Any) -> dict[str, Any]:
        return self._return("diff_versions", kwargs)


def _error_code(result: Any) -> str:
    return result.to_dict()["error"]["data"]["code"]


def test_chunk_version_list_returns_paginated_stable_summaries() -> None:
    created_at = datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc)
    boundary = FakeChunkVersionBoundary(
        {
            "chunk_id": CHUNK_ID,
            "items": [
                {
                    "id": "550e8400-e29b-41d4-a716-446655440010",
                    "version_no": 1,
                    "text": "first version",
                    "created_at": created_at,
                    "is_current": False,
                    "status": "retired",
                    "char_count": 13,
                    "text_sha256": "checksum-1",
                    "comment": "initial",
                },
                {
                    "id": "550e8400-e29b-41d4-a716-446655440011",
                    "version_no": 2,
                    "preview": "second preview",
                    "created_at": "2026-07-18T13:00:00+00:00",
                    "current": True,
                    "status": "active",
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
            include_deleted=True,
            limit=1,
            offset=1,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert result.data["items"] == [
        {
            "id": "550e8400-e29b-41d4-a716-446655440011",
            "logical_chunk_id": None,
            "version_no": 2,
            "preview": "second preview",
            "created_at": "2026-07-18T13:00:00+00:00",
            "current": True,
            "status": "active",
            "valid_from": None,
            "valid_to": None,
            "char_count": 15,
            "checksum": "checksum-2",
            "text_sha256": "checksum-2",
            "comment": "edited",
            "actor": None,
            "operation": None,
            "previous_version_id": None,
            "restored_from_version_id": None,
        }
    ]
    assert result.data["total"] == 2
    assert result.data["offset"] == 1
    assert result.data["limit"] == 1
    assert boundary.calls == [("list_versions", {"chunk_id": CHUNK_ID, "include_deleted": True})]


def test_chunk_history_routes_to_history_boundary() -> None:
    boundary = FakeChunkVersionBoundary({"chunk_id": CHUNK_ID, "items": [], "total": 0})

    result = asyncio.run(
        ChunkHistoryCommand().execute(
            chunk_id=CHUNK_ID,
            include_deleted=False,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls == [("history", {"chunk_id": CHUNK_ID, "include_deleted": False})]


def test_chunk_version_get_routes_current_selector() -> None:
    boundary = FakeChunkVersionBoundary({"chunk_id": CHUNK_ID, "version": {"version_no": 2, "text": "body"}})

    result = asyncio.run(
        ChunkVersionGetCommand().execute(
            chunk_id=CHUNK_ID,
            current=True,
            include_text=True,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls == [
        (
            "get_version",
            {"chunk_id": CHUNK_ID, "version_no": None, "current": True, "include_text": True},
        )
    ]


@pytest.mark.parametrize(
    ("command", "expected_method"),
    [
        (ChunkVersionAddCommand(), "append_version"),
        (ChunkVersionUpdateCommand(), "update_text"),
    ],
)
def test_chunk_version_text_mutations_route_with_optimistic_lock(command: Any, expected_method: str) -> None:
    boundary = FakeChunkVersionBoundary({"chunk_id": CHUNK_ID, "outcome": "appended", "version": {"version_no": 2}})

    result = asyncio.run(
        command.execute(
            chunk_id=CHUNK_ID,
            text="new body",
            comment="edited",
            actor="tester",
            expected_current_version=1,
            operation_id=OPERATION_ID,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls == [
        (
            expected_method,
            {
                "chunk_id": CHUNK_ID,
                "text_value": "new body",
                "comment": "edited",
                "actor": "tester",
                "expected_current_version": 1,
                "operation_id": OPERATION_ID,
            },
        )
    ]


def test_chunk_version_restore_routes_with_audit_fields() -> None:
    boundary = FakeChunkVersionBoundary({"chunk_id": CHUNK_ID, "outcome": "restored", "version": {"version_no": 3}})

    result = asyncio.run(
        ChunkVersionRestoreCommand().execute(
            chunk_id=CHUNK_ID,
            version_no=1,
            comment="restore",
            actor="tester",
            expected_current_version=2,
            operation_id=OPERATION_ID,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls == [
        (
            "restore_version",
            {
                "chunk_id": CHUNK_ID,
                "version_no": 1,
                "comment": "restore",
                "actor": "tester",
                "expected_current_version": 2,
                "operation_id": OPERATION_ID,
            },
        )
    ]


def test_chunk_version_retire_routes_replacement() -> None:
    boundary = FakeChunkVersionBoundary({"chunk_id": CHUNK_ID, "outcome": "retired", "retired_version_no": 1})

    result = asyncio.run(
        ChunkVersionRetireCommand().execute(
            chunk_id=CHUNK_ID,
            version_no=1,
            replacement_version_no=2,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls == [
        (
            "retire_version",
            {
                "chunk_id": CHUNK_ID,
                "version_no": 1,
                "replacement_version_no": 2,
                "comment": None,
                "actor": None,
            },
        )
    ]


def test_chunk_version_diff_routes_pair() -> None:
    boundary = FakeChunkVersionBoundary({"chunk_id": CHUNK_ID, "diff": ["--- v1", "+++ v2"], "changed": True})

    result = asyncio.run(
        ChunkVersionDiffCommand().execute(
            chunk_id=CHUNK_ID,
            from_version_no=1,
            to_version_no=2,
            context_lines=4,
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls == [
        (
            "diff_versions",
            {
                "chunk_id": CHUNK_ID,
                "from_version_no": 1,
                "to_version_no": 2,
                "context_lines": 4,
            },
        )
    ]


def test_chunk_version_set_current_routes_boundary_call() -> None:
    boundary = FakeChunkVersionBoundary({"chunk_id": CHUNK_ID, "outcome": "set_current", "version": {"version_no": 7}})

    result = asyncio.run(
        ChunkVersionSetCurrentCommand().execute(
            chunk_id=CHUNK_ID,
            version_no=7,
            comment="rollback",
            actor="tester",
            context={"chunk_version_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls == [("set_current", {"chunk_id": CHUNK_ID, "version_no": 7, "comment": "rollback", "actor": "tester"})]


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


@pytest.mark.parametrize(
    ("command", "kwargs"),
    [
        (ChunkVersionListCommand(), {"chunk_id": "not-a-uuid"}),
        (ChunkHistoryCommand(), {"chunk_id": "not-a-uuid"}),
        (ChunkVersionGetCommand(), {"chunk_id": "not-a-uuid"}),
        (ChunkVersionAddCommand(), {"chunk_id": "not-a-uuid", "text": "x"}),
        (ChunkVersionUpdateCommand(), {"chunk_id": "not-a-uuid", "text": "x"}),
        (ChunkVersionSetCurrentCommand(), {"chunk_id": "not-a-uuid", "version_no": 1}),
        (ChunkVersionRestoreCommand(), {"chunk_id": "not-a-uuid", "version_no": 1}),
        (ChunkVersionRetireCommand(), {"chunk_id": "not-a-uuid", "version_no": 1}),
        (ChunkVersionDiffCommand(), {"chunk_id": "not-a-uuid", "from_version_no": 1, "to_version_no": 2}),
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


@pytest.mark.parametrize(
    "command",
    [
        ChunkVersionSetCurrentCommand(),
        ChunkVersionRestoreCommand(),
        ChunkVersionRetireCommand(),
        ChunkVersionDeleteCommand(),
    ],
)
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


def test_chunk_version_diff_rejects_invalid_context_lines() -> None:
    result = asyncio.run(
        ChunkVersionDiffCommand().execute(
            chunk_id=CHUNK_ID,
            from_version_no=1,
            to_version_no=2,
            context_lines=21,
            context={"chunk_version_boundary": FakeChunkVersionBoundary()},
        )
    )

    assert _error_code(result) == "INVALID_PARAMS"


@pytest.mark.parametrize(
    ("error", "expected_code", "command"),
    [
        (ChunkTextVersionError("VERSION_NOT_FOUND", "version missing"), "NOT_FOUND", ChunkVersionGetCommand()),
        (
            ChunkTextVersionError(LAST_VERSION_DELETE_CODE, "delete the chunk instead of deleting its last text version"),
            "LAST_VERSION_DELETE_FORBIDDEN",
            ChunkVersionDeleteCommand(),
        ),
        (
            ChunkTextVersionError(CURRENT_VERSION_RETIRE_CODE, "replacement required"),
            "CURRENT_VERSION_RETIRE_REQUIRES_REPLACEMENT",
            ChunkVersionRetireCommand(),
        ),
        (
            ChunkTextVersionError(CURRENT_VERSION_MISMATCH_CODE, "current mismatch"),
            "CURRENT_VERSION_MISMATCH",
            ChunkVersionUpdateCommand(),
        ),
    ],
)
def test_chunk_version_runtime_errors_map_stably(error: Exception, expected_code: str, command: Any) -> None:
    boundary = FakeChunkVersionBoundary(error)
    kwargs = {"chunk_id": CHUNK_ID}
    if isinstance(command, (ChunkVersionDeleteCommand, ChunkVersionRetireCommand)):
        kwargs["version_no"] = 1
    elif isinstance(command, ChunkVersionUpdateCommand):
        kwargs["text"] = "x"

    result = asyncio.run(command.execute(**kwargs, context={"chunk_version_boundary": boundary}))

    assert _error_code(result) == expected_code


@pytest.mark.parametrize(
    ("command", "kwargs"),
    [
        (ChunkVersionListCommand(), {"chunk_id": CHUNK_ID}),
        (ChunkHistoryCommand(), {"chunk_id": CHUNK_ID}),
        (ChunkVersionGetCommand(), {"chunk_id": CHUNK_ID}),
        (ChunkVersionAddCommand(), {"chunk_id": CHUNK_ID, "text": "x"}),
        (ChunkVersionUpdateCommand(), {"chunk_id": CHUNK_ID, "text": "x"}),
        (ChunkVersionSetCurrentCommand(), {"chunk_id": CHUNK_ID, "version_no": 1}),
        (ChunkVersionRestoreCommand(), {"chunk_id": CHUNK_ID, "version_no": 1}),
        (ChunkVersionRetireCommand(), {"chunk_id": CHUNK_ID, "version_no": 1}),
        (ChunkVersionDiffCommand(), {"chunk_id": CHUNK_ID, "from_version_no": 1, "to_version_no": 2}),
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
