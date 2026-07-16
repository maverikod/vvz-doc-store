"""Focused contracts for corpus audit command."""

from __future__ import annotations

import asyncio
from typing import Any

from doc_store_server.commands.corpus_audit_command import CorpusAuditCommand
from doc_store_server.runtime.semantic_relations import unit_title_edit_capabilities


class Boundary:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def audit(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "status": "ok",
            "mode": kwargs["mode"],
            "scope": {"project": kwargs["project"]},
            "items": [],
            "groups": [],
            "pagination": {"limit": kwargs["limit"], "offset": kwargs["offset"], "total": 0},
            "diagnostics": {"unit_title_editing": unit_title_edit_capabilities()},
        }


def test_schema_and_metadata_cover_all_audit_modes() -> None:
    schema = CorpusAuditCommand.get_schema()
    metadata = CorpusAuditCommand.metadata()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["mode"]["enum"] == [
        "inventory",
        "corrections",
        "conflicts",
        "exact_duplicates",
        "topics",
        "unit_title_capabilities",
    ]
    assert metadata["name"] == "corpus_audit"
    assert "unit_title_capabilities" in metadata["detailed_description"]
    assert metadata["parameters"] == schema["properties"]


def test_execute_delegates_to_injected_boundary() -> None:
    boundary = Boundary()

    result = asyncio.run(
        CorpusAuditCommand().execute(
            mode="corrections",
            project="7d",
            markers=["корректировка"],
            min_length=120,
            include_aggregators=True,
            limit=7,
            offset=2,
            context={"corpus_audit_boundary": boundary},
        )
    )

    assert result.success is True
    assert result.data["mode"] == "corrections"
    assert boundary.calls == [
        {
            "mode": "corrections",
            "project": "7d",
            "document_id": None,
            "source_name": None,
            "seven_d_number": None,
            "markers": ["корректировка"],
            "min_length": 120,
            "include_aggregators": True,
            "include_deleted": False,
            "limit": 7,
            "offset": 2,
        }
    ]


def test_unit_title_capabilities_state_current_public_write_support() -> None:
    capabilities = unit_title_edit_capabilities()

    assert capabilities["documents"]["supported"] is True
    assert capabilities["documents"]["editable_via"] == "entity_update"
    assert capabilities["chapters"]["direct_field"] == "heading"
    assert capabilities["chapters"]["supported"] is False
    assert capabilities["paragraphs"]["metadata_field"] == "block_meta.title"
    assert capabilities["semantic_chunks"]["metadata_field"] == "block_meta.title"


def test_execute_reports_boundary_unavailable_and_invalid_params(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "doc_store_server.commands.corpus_audit_command.installed_corpus_audit_service",
        lambda: None,
    )
    missing = asyncio.run(CorpusAuditCommand().execute())
    assert missing.to_dict()["error"]["data"]["code"] == "CORPUS_AUDIT_BOUNDARY_UNAVAILABLE"

    class FailingBoundary:
        def audit(self, **_: Any) -> dict[str, Any]:
            raise ValueError("bad mode")

    invalid = asyncio.run(
        CorpusAuditCommand().execute(context={"corpus_audit_boundary": FailingBoundary()})
    )
    assert invalid.to_dict()["error"]["data"]["code"] == "INVALID_PARAMS"
