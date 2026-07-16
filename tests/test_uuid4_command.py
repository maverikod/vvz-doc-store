"""Contract tests for UUID4 utility command and strict UUID4 validation."""

from __future__ import annotations

import asyncio
from uuid import UUID

from doc_store_server.commands.uuid4_command import Uuid4Command


def test_uuid4_command_returns_single_uuid4_by_default() -> None:
    result = asyncio.run(Uuid4Command().execute())

    assert result.success is True
    identifier = UUID(result.data["uuid4"])
    assert identifier.version == 4
    assert result.data["count"] == 1
    assert "uuid4_list" not in result.data


def test_uuid4_command_returns_uuid4_batch() -> None:
    result = asyncio.run(Uuid4Command().execute(count=3))

    assert result.success is True
    values = result.data["uuid4_list"]
    assert len(values) == 3
    assert len(set(values)) == 3
    assert all(UUID(value).version == 4 for value in values)


def test_uuid4_command_rejects_invalid_count() -> None:
    result = asyncio.run(Uuid4Command().execute(count=0))

    assert result.to_dict()["success"] is False
    assert result.to_dict()["error"]["data"]["code"] == "INVALID_PARAMS"
