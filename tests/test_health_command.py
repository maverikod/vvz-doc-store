from __future__ import annotations

import asyncio
from types import SimpleNamespace

from doc_store_server.commands import health_command
from doc_store_server.commands.health_command import DocStoreHealthCommand


def test_health_includes_vectorizer_current_file(monkeypatch) -> None:
    monkeypatch.setattr(
        health_command,
        "installed_runtime_status",
        lambda: SimpleNamespace(
            snapshot=lambda: {
                "state": "idle",
                "current_activity": None,
                "last_activity": None,
            }
        ),
    )
    monkeypatch.setattr(
        health_command,
        "installed_vectorization_snapshot",
        lambda _config: {
            "state": "running",
            "current_activity": {
                "action": "embedding_documents",
                "current_document_id": "document-1",
                "current_file": "/docs/example.md",
            },
            "last_activity": None,
        },
    )

    result = asyncio.run(DocStoreHealthCommand().execute())

    assert result.data["components"]["worker"]["vectorizer"]["state"] == "running"
    assert (
        result.data["components"]["worker"]["vectorizer"]["current_activity"]["current_file"]
        == "/docs/example.md"
    )
