"""Tests for installed runtime document service helpers."""

from __future__ import annotations

import pytest

from doc_store_server.runtime.document_service import (
    RuntimeDocumentService,
    _version_token_matches,
)


@pytest.mark.parametrize(
    "token",
    ("7", "document-version-7", "etag:7"),
)
def test_version_token_matches_supported_runtime_forms(token: str) -> None:
    assert _version_token_matches(token, 7) is True


def test_version_token_rejects_stale_value() -> None:
    assert _version_token_matches("6", 7) is False


def test_delete_document_requires_database_url() -> None:
    service = RuntimeDocumentService(None)

    with pytest.raises(RuntimeError, match="database URL is not configured"):
        service.delete_document("550e8400-e29b-41d4-a716-446655440000", "1")
