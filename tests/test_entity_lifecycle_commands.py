"""Focused tests for addressable entity lifecycle commands."""

from __future__ import annotations

import asyncio
from typing import Any

from doc_store_server.commands.entity_lifecycle_commands import (
    EntityGetCommand,
    EntityHardDeleteCommand,
    EntityListCommand,
    EntityReferencesCommand,
    EntitySoftDeleteCommand,
    EntityUndeleteCommand,
)
from doc_store_server.runtime.entity_lifecycle import DeletionSafetyError, ORDER_BY


class Boundary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_entities(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list", kwargs))
        return {"entity_type": kwargs["entity_type"], "items": [], "limit": kwargs["limit"], "offset": kwargs["offset"], "total": 0, "show_deleted": kwargs["show_deleted"]}

    def get_entity(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        return {"entity_type": kwargs["entity_type"], "id": kwargs["entity_id"], "value": {"id": kwargs["entity_id"]}}

    def soft_delete(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("soft_delete", kwargs))
        return {"outcome": "updated", "updated": {"documents": len(kwargs["ids"])}, "is_deleted": True}

    def undelete(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("undelete", kwargs))
        return {"outcome": "updated", "updated": {"documents": len(kwargs["ids"])}, "is_deleted": False}

    def hard_delete(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("hard_delete", kwargs))
        return {"outcome": "deleted", "deleted": {"documents": len(kwargs["ids"])}, "blocked": []}

    def references_for(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("references", kwargs))
        return {"entity_type": kwargs["entity_type"], "id": kwargs["entity_id"], "references": []}


def test_list_get_and_references_delegate_to_lifecycle_boundary() -> None:
    boundary = Boundary()
    list_result = asyncio.run(
        EntityListCommand().execute(
            entity_type="documents",
            fields=["id", "title"],
            limit=5,
            offset=1,
            context={"entity_lifecycle_boundary": boundary},
        )
    )
    get_result = asyncio.run(
        EntityGetCommand().execute(
            entity_type="documents",
            entity_id="550e8400-e29b-41d4-a716-446655440001",
            context={"entity_lifecycle_boundary": boundary},
        )
    )
    refs_result = asyncio.run(
        EntityReferencesCommand().execute(
            entity_type="documents",
            entity_id="550e8400-e29b-41d4-a716-446655440001",
            context={"entity_lifecycle_boundary": boundary},
        )
    )

    assert list_result.success is True
    assert get_result.success is True
    assert refs_result.success is True
    assert [name for name, _ in boundary.calls] == ["list", "get", "references"]


def test_batch_soft_undelete_and_hard_delete_delegate_to_lifecycle_boundary() -> None:
    boundary = Boundary()
    params = {
        "entity_type": "documents",
        "ids": ["550e8400-e29b-41d4-a716-446655440001"],
        "context": {"entity_lifecycle_boundary": boundary},
    }

    assert asyncio.run(EntitySoftDeleteCommand().execute(**params)).data["is_deleted"] is True
    assert asyncio.run(EntityUndeleteCommand().execute(**params)).data["is_deleted"] is False
    assert asyncio.run(EntityHardDeleteCommand().execute(**params)).data["outcome"] == "deleted"

    assert [name for name, _ in boundary.calls] == ["soft_delete", "undelete", "hard_delete"]


def test_hard_delete_safety_error_is_public_delete_blocked_error() -> None:
    class BlockingBoundary(Boundary):
        def hard_delete(self, **kwargs: Any) -> dict[str, Any]:
            raise DeletionSafetyError("external references exist")

    result = asyncio.run(
        EntityHardDeleteCommand().execute(
            entity_type="documents",
            ids=["550e8400-e29b-41d4-a716-446655440001"],
            context={"entity_lifecycle_boundary": BlockingBoundary()},
        )
    )

    assert result.to_dict()["success"] is False
    assert result.to_dict()["error"]["data"]["code"] == "DELETE_BLOCKED"


def test_missing_lifecycle_boundary_is_stable_error(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "doc_store_server.commands.entity_lifecycle_commands.installed_entity_lifecycle_service",
        lambda: None,
    )
    result = asyncio.run(EntityListCommand().execute(entity_type="documents"))

    assert result.to_dict()["success"] is False
    assert result.to_dict()["error"]["data"]["code"] == "LIFECYCLE_BOUNDARY_UNAVAILABLE"


def test_document_listing_does_not_require_unit_order_column() -> None:
    assert "order_index" not in ORDER_BY["documents"]
    assert "order_index" in ORDER_BY["paragraphs"]
    assert "order_index" in ORDER_BY["semantic_chunks"]
