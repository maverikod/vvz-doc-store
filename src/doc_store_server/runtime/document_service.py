"""Installed runtime service for canonical document operations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from doc_store_server.db.health import database_url_from_config


class RuntimeDocumentService:
    """Delete canonical documents through the installed PostgreSQL boundary."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    def delete_document(self, document_id: str, version_token: str) -> dict[str, str]:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")

        engine = create_engine(self._database_url)
        try:
            with engine.begin() as connection:
                row = connection.execute(
                    text(
                        "SELECT id::text AS document_id, source_version "
                        "FROM documents WHERE id::text = :document_id"
                    ),
                    {"document_id": document_id},
                ).mappings().first()
                if row is None:
                    return {"outcome": "already_absent", "document_id": document_id}

                source_version = row["source_version"]
                if not _version_token_matches(version_token, source_version):
                    return {"outcome": "conflict", "document_id": document_id}

                connection.execute(
                    text("DELETE FROM documents WHERE id::text = :document_id"),
                    {"document_id": document_id},
                )
        except SQLAlchemyError as exc:
            raise RuntimeError("document delete failed") from exc
        finally:
            engine.dispose()
        return {"outcome": "deleted", "document_id": document_id}


def _version_token_matches(version_token: str, source_version: Any) -> bool:
    raw = str(source_version)
    accepted = {
        raw,
        f"document-version-{raw}",
        f"etag:{raw}",
    }
    return version_token.strip() in accepted


def installed_document_service(config: dict[str, Any]) -> RuntimeDocumentService:
    """Build the installed document service from runtime config."""

    return RuntimeDocumentService(database_url_from_config(config))


__all__ = ["RuntimeDocumentService", "installed_document_service"]
