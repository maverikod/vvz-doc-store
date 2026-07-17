"""Focused tests for addressable entity lifecycle commands."""

from __future__ import annotations

import asyncio
from typing import Any

from doc_store_server.commands.entity_lifecycle_commands import (
    EntityCreateCommand,
    EntityGetCommand,
    EntityHardDeleteCommand,
    EntityListCommand,
    EntityOwnerTreeCommand,
    EntityRebindOwnerCommand,
    EntityReferencesCommand,
    EntitySoftDeleteCommand,
    EntityUpdateCommand,
    EntityUndeleteCommand,
)
from doc_store_server.runtime.entity_lifecycle import DeletionSafetyError, ORDER_BY


class Boundary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_entities(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list", kwargs))
        return {"entity_type": kwargs["entity_type"], "items": [], "limit": kwargs["limit"], "offset": kwargs["offset"], "total": 0, "show_deleted": kwargs["show_deleted"]}

    def create_entity(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create", kwargs))
        return {"entity_type": kwargs["entity_type"], "outcome": "created", "value": dict(kwargs["values"])}

    def update_entity(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("update", kwargs))
        return {"entity_type": kwargs["entity_type"], "outcome": "updated", "value": dict(kwargs["values"])}

    def rebind_owner(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("rebind_owner", kwargs))
        return {
            "entity_type": kwargs["entity_type"],
            "owner_id": kwargs["owner_id"],
            "requested": len(kwargs["ids"]),
            "updated": len(kwargs["ids"]),
            "items": [],
        }

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

    def owner_tree(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("owner_tree", kwargs))
        return {
            "entity_type": kwargs.get("entity_type") or "documents",
            "id": kwargs["entity_id"],
            "max_depth": kwargs["max_depth"],
            "max_children_per_node": kwargs["max_children_per_node"],
            "include_deleted": kwargs["include_deleted"],
            "tree": {"id": kwargs["entity_id"], "preview": "doc", "children": []},
        }


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


def test_owner_tree_delegates_uuid4_checked_request() -> None:
    boundary = Boundary()
    result = asyncio.run(
        EntityOwnerTreeCommand().execute(
            entity_id="550e8400-e29b-41d4-a716-446655440001",
            entity_type="documents",
            max_depth=3,
            max_children_per_node=25,
            include_deleted=True,
            context={"entity_lifecycle_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls == [
        (
            "owner_tree",
            {
                "entity_id": "550e8400-e29b-41d4-a716-446655440001",
                "entity_type": "documents",
                "max_depth": 3,
                "max_children_per_node": 25,
                "include_deleted": True,
            },
        )
    ]
    assert result.data["tree"]["preview"] == "doc"


def test_dictionary_create_and_update_use_generic_entity_boundary() -> None:
    boundary = Boundary()
    create_result = asyncio.run(
        EntityCreateCommand().execute(
            entity_type="categories",
            values={"id": "550e8400-e29b-41d4-a716-446655440001", "descr": "theory"},
            context={"entity_lifecycle_boundary": boundary},
        )
    )
    update_result = asyncio.run(
        EntityUpdateCommand().execute(
            entity_type="categories",
            entity_id="550e8400-e29b-41d4-a716-446655440001",
            values={"descr": "theory-updated"},
            context={"entity_lifecycle_boundary": boundary},
        )
    )

    assert create_result.success is True
    assert update_result.success is True
    assert [name for name, _ in boundary.calls] == ["create", "update"]


def test_entity_update_accepts_paragraph_and_sentence_text_targets() -> None:
    boundary = Boundary()
    paragraph_result = asyncio.run(
        EntityUpdateCommand().execute(
            entity_type="paragraphs",
            entity_id="550e8400-e29b-41d4-a716-446655440001",
            values={"text": "Updated paragraph."},
            context={"entity_lifecycle_boundary": boundary},
        )
    )
    sentence_result = asyncio.run(
        EntityUpdateCommand().execute(
            entity_type="semantic_chunks",
            entity_id="550e8400-e29b-41d4-a716-446655440002",
            values={"text": "Updated sentence."},
            context={"entity_lifecycle_boundary": boundary},
        )
    )

    assert paragraph_result.success is True
    assert sentence_result.success is True
    assert boundary.calls[-2:] == [
        (
            "update",
            {
                "entity_type": "paragraphs",
                "entity_id": "550e8400-e29b-41d4-a716-446655440001",
                "values": {"text": "Updated paragraph."},
            },
        ),
        (
            "update",
            {
                "entity_type": "semantic_chunks",
                "entity_id": "550e8400-e29b-41d4-a716-446655440002",
                "values": {"text": "Updated sentence."},
            },
        ),
    ]
    assert "paragraphs" in EntityUpdateCommand.get_schema()["properties"]["entity_type"]["enum"]
    assert "semantic_chunks" in EntityUpdateCommand.get_schema()["properties"]["entity_type"]["enum"]


def test_rebind_owner_delegates_uuid4_checked_batch() -> None:
    boundary = Boundary()
    result = asyncio.run(
        EntityRebindOwnerCommand().execute(
            entity_type="files",
            ids=["550e8400-e29b-41d4-a716-446655440001"],
            owner_id="550e8400-e29b-41d4-a716-446655440002",
            context={"entity_lifecycle_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls == [
        (
            "rebind_owner",
            {
                "entity_type": "files",
                "ids": ["550e8400-e29b-41d4-a716-446655440001"],
                "owner_id": "550e8400-e29b-41d4-a716-446655440002",
            },
        )
    ]


def test_rebind_owner_accepts_null_owner_for_unbinding() -> None:
    boundary = Boundary()
    result = asyncio.run(
        EntityRebindOwnerCommand().execute(
            entity_type="semantic_chunks",
            ids=["550e8400-e29b-41d4-a716-446655440001"],
            owner_id=None,
            context={"entity_lifecycle_boundary": boundary},
        )
    )

    assert result.success is True
    assert boundary.calls[0][1]["owner_id"] is None


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


def test_entity_commands_reject_non_v4_uuid_fields_before_boundary_delegation() -> None:
    boundary = Boundary()
    v1_uuid = "550e8400-e29b-11d4-a716-446655440001"

    get_result = asyncio.run(
        EntityGetCommand().execute(
            entity_type="documents",
            entity_id=v1_uuid,
            context={"entity_lifecycle_boundary": boundary},
        )
    )
    create_result = asyncio.run(
        EntityCreateCommand().execute(
            entity_type="files",
            values={"id": v1_uuid, "path": "/tmp/doc.txt", "name": "doc.txt", "body_sha256": "0" * 64},
            context={"entity_lifecycle_boundary": boundary},
        )
    )
    rebind_result = asyncio.run(
        EntityRebindOwnerCommand().execute(
            entity_type="files",
            ids=["550e8400-e29b-41d4-a716-446655440001"],
            owner_id=v1_uuid,
            context={"entity_lifecycle_boundary": boundary},
        )
    )
    owner_tree_result = asyncio.run(
        EntityOwnerTreeCommand().execute(
            entity_id=v1_uuid,
            context={"entity_lifecycle_boundary": boundary},
        )
    )

    assert get_result.to_dict()["success"] is False
    assert create_result.to_dict()["success"] is False
    assert rebind_result.to_dict()["success"] is False
    assert owner_tree_result.to_dict()["success"] is False
    assert get_result.to_dict()["error"]["data"]["code"] == "INVALID_PARAMS"
    assert create_result.to_dict()["error"]["data"]["code"] == "INVALID_PARAMS"
    assert rebind_result.to_dict()["error"]["data"]["code"] == "INVALID_PARAMS"
    assert owner_tree_result.to_dict()["error"]["data"]["code"] == "INVALID_PARAMS"
    assert boundary.calls == []


def test_document_listing_does_not_require_unit_order_column() -> None:
    assert "order_index" not in ORDER_BY["documents"]
    assert "order_index" in ORDER_BY["paragraphs"]
    assert "order_index" in ORDER_BY["semantic_chunks"]


def test_dictionary_ordering_is_generic_descr_order() -> None:
    assert ORDER_BY["categories"] == "descr ASC, id ASC"
    assert ORDER_BY["languages"] == "descr ASC, id ASC"
