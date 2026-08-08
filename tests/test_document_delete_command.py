"""Focused contract tests for the canonical document deletion command."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp_proxy_adapter.core.errors import ValidationError

from doc_store_server.commands.document_delete_command import DocumentDeleteCommand


DOCUMENT_ID = "550e8400-e29b-41d4-a716-446655440000"
VERSION_TOKEN = "document-version-7"


class RecordingCanonicalService:
    """A narrow service double that exposes the command's only valid boundary."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def delete_document(self, document_id: str, version_token: str) -> Any:
        self.calls.append((document_id, version_token))
        if self.error is not None:
            raise self.error
        return self.response


def _run(command: DocumentDeleteCommand, **params: Any) -> Any:
    """Run the async command without requiring an async pytest plugin."""

    return asyncio.run(command.execute(**params))


@pytest.mark.parametrize("response", [{"outcome": "deleted"}, "deleted"])
def test_valid_deletion_delegates_atomically_to_canonical_service(response: Any) -> None:
    service = RecordingCanonicalService(response=response)

    result = _run(
        DocumentDeleteCommand(),
        document_id=DOCUMENT_ID,
        version_token=VERSION_TOKEN,
        context={"canonical_document_service": service},
    )

    assert result.to_dict() == {
        "success": True,
        "data": {"outcome": "deleted", "document_id": DOCUMENT_ID},
    }
    assert service.calls == [(DOCUMENT_ID, VERSION_TOKEN)]


def test_already_absent_is_idempotent_success() -> None:
    service = RecordingCanonicalService(response={"status": "already_absent"})

    result = _run(
        DocumentDeleteCommand(),
        document_id=DOCUMENT_ID,
        version_token=VERSION_TOKEN,
        context={"document_service": service},
    )

    assert result.to_dict() == {
        "success": True,
        "data": {"outcome": "already_absent", "document_id": DOCUMENT_ID},
    }
    assert service.calls == [(DOCUMENT_ID, VERSION_TOKEN)]


def test_a_stale_precondition_is_a_stable_conflict() -> None:
    """A service that reports conflict is reported as conflict, unchanged."""

    service = RecordingCanonicalService(response="conflict", error=None)

    result = _run(
        DocumentDeleteCommand(),
        document_id=DOCUMENT_ID,
        version_token="stale-token",
        context={"canonical_document_service": service},
    )

    assert result.to_dict() == {
        "success": False,
        "data": {"outcome": "conflict", "document_id": DOCUMENT_ID},
    }
    assert service.calls == [(DOCUMENT_ID, "stale-token")]


def test_a_raised_failure_names_its_cause_instead_of_claiming_a_conflict() -> None:
    """Superseded: this case used to assert that any exception became conflict.

    That was the defect, not the contract. It told operators the token was stale
    and to retry with a current one, while the real cause was a database-level
    refusal that no token could satisfy -- migration 0019's own SET NULL foreign
    key colliding with its immutability trigger. The failure now carries the
    exception type and message so it can be acted on.
    """

    service = RecordingCanonicalService(
        response=None, error=RuntimeError("document delete failed")
    )

    result = _run(
        DocumentDeleteCommand(),
        document_id=DOCUMENT_ID,
        version_token="current-token",
        context={"canonical_document_service": service},
    )

    payload = result.to_dict()
    assert payload["success"] is False
    assert payload["data"] == {"outcome": "delete_failed", "document_id": DOCUMENT_ID}
    assert "RuntimeError" in payload["error"]
    assert "document delete failed" in payload["error"]
    assert "precondition" not in payload["error"]
    assert service.calls == [(DOCUMENT_ID, "current-token")]


def test_every_outcome_the_command_can_return_is_declared() -> None:
    """The declared enum used to describe less than the command returns.

    ``_failure`` projects its error code into the outcome, so ``invalid_params``
    and ``service_unavailable`` were already reachable while the schema listed
    only the three service outcomes.
    """

    declared = DocumentDeleteCommand.metadata()["return_value"]["schema"]
    enum = declared["properties"]["outcome"]["enum"]
    assert set(DocumentDeleteCommand.service_outcomes) <= set(enum)
    for reachable in ("invalid_params", "service_unavailable", "delete_failed"):
        assert reachable in enum, reachable
    # The inverse: what the service itself may report stays exactly three.
    assert DocumentDeleteCommand.service_outcomes == (
        "deleted",
        "already_absent",
        "conflict",
    )


def test_missing_service_is_a_stable_unavailable_result() -> None:
    result = _run(
        DocumentDeleteCommand(),
        document_id=DOCUMENT_ID,
        version_token=VERSION_TOKEN,
    )

    assert result.to_dict() == {
        "success": False,
        "data": {"outcome": "service_unavailable", "document_id": DOCUMENT_ID},
        "error": "Canonical document service is unavailable.",
    }


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"document_id": "", "version_token": VERSION_TOKEN},
        {"document_id": "doc-123", "version_token": VERSION_TOKEN},
        {"document_id": "550e8400-e29b-11d4-a716-446655440000", "version_token": VERSION_TOKEN},
        {"document_id": DOCUMENT_ID, "version_token": "  "},
        {"document_id": 123, "version_token": VERSION_TOKEN},
        {"document_id": DOCUMENT_ID, "version_token": None},
        {"document_id": DOCUMENT_ID, "version_token": VERSION_TOKEN, "extra": True},
    ],
)
def test_invalid_and_unknown_parameters_are_rejected(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocumentDeleteCommand().validate_params(params)


def test_validation_strips_only_surrounding_whitespace_from_identifiers() -> None:
    assert DocumentDeleteCommand().validate_params(
        {"document_id": f"  {DOCUMENT_ID} ", "version_token": " token-7 "}
    ) == {"document_id": DOCUMENT_ID, "version_token": "token-7"}


def test_result_serialization_is_stable_and_does_not_leak_service_payload() -> None:
    service = RecordingCanonicalService(
        response={"outcome": "deleted", "sql": "DROP TABLE chapters", "internal": object()}
    )

    result = _run(
        DocumentDeleteCommand(),
        document_id=DOCUMENT_ID,
        version_token=VERSION_TOKEN,
        context={"document_service": service},
    )

    assert result.to_dict() == {
        "success": True,
        "data": {"outcome": "deleted", "document_id": DOCUMENT_ID},
    }


def test_schema_and_metadata_are_complete_and_strict() -> None:
    schema = DocumentDeleteCommand.get_schema()
    metadata = DocumentDeleteCommand.metadata()

    assert schema == {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "minLength": 1,
                "description": "Canonical document UUID4 identifier to delete.",
            },
            "version_token": {
                "type": "string",
                "minLength": 1,
                "description": "Required version or deletion precondition token; the service must reject a stale token.",
            },
        },
        "required": ["document_id", "version_token"],
        "additionalProperties": False,
    }
    assert {
        "name",
        "version",
        "description",
        "category",
        "author",
        "email",
        "detailed_description",
        "parameters",
        "return_value",
        "usage_examples",
        "error_cases",
        "best_practices",
    } == set(metadata)
    assert metadata["name"] == "document_delete"
    assert metadata["parameters"] == schema["properties"]
    assert metadata["return_value"]["schema"]["additionalProperties"] is False
    assert metadata["return_value"]["schema"]["required"] == ["outcome", "document_id"]
    # Superseded: this pinned the declared enum to the three outcomes the
    # canonical service may report, but the command's own _failure projects its
    # error code into the outcome, so invalid_params and service_unavailable
    # were already reachable and undeclared. The declaration now covers every
    # outcome that can reach the wire; what the service itself may report is a
    # separate, still-exactly-three vocabulary asserted below.
    assert set(metadata["return_value"]["schema"]["properties"]["outcome"]["enum"]) == {
        "deleted",
        "already_absent",
        "conflict",
        "invalid_params",
        "service_unavailable",
        "delete_failed",
    }
    assert DocumentDeleteCommand.service_outcomes == (
        "deleted",
        "already_absent",
        "conflict",
    )
    assert {"INVALID_PARAMS", "CONFLICT", "SERVICE_UNAVAILABLE"} <= set(
        metadata["error_cases"]
    )


def test_command_uses_only_canonical_service_boundary() -> None:
    service = RecordingCanonicalService(response="deleted")
    forbidden_calls: list[str] = []

    class ForbiddenDirectPath:
        def __getattr__(self, name: str) -> Any:
            forbidden_calls.append(name)
            raise AssertionError(f"direct deletion path invoked: {name}")

    result = _run(
        DocumentDeleteCommand(),
        document_id=DOCUMENT_ID,
        version_token=VERSION_TOKEN,
        context={
            "canonical_document_service": service,
            "chapter_repository": ForbiddenDirectPath(),
            "chunk_repository": ForbiddenDirectPath(),
            "sql": ForbiddenDirectPath(),
            "transaction": ForbiddenDirectPath(),
            "transport": ForbiddenDirectPath(),
            "queue": ForbiddenDirectPath(),
            "rest": ForbiddenDirectPath(),
        },
    )

    assert result.to_dict() == {
        "success": True,
        "data": {"outcome": "deleted", "document_id": DOCUMENT_ID},
    }
    assert service.calls == [(DOCUMENT_ID, VERSION_TOKEN)]
    assert forbidden_calls == []
