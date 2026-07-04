"""doc-store FastAPI entry point."""

from __future__ import annotations

from fastapi import FastAPI

from doc_store_server.api.documents import router as documents_router


def create_app() -> FastAPI:
    app = FastAPI(title="doc-store", version="0.1.0")
    app.include_router(documents_router, prefix="/api/v1/documents", tags=["documents"])
    return app


app = create_app()
