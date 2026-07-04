"""Document ingestion API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, UploadFile
from pydantic import BaseModel, Field

router = APIRouter()


class TextUploadRequest(BaseModel):
    text: str
    source_name: str
    metadata: dict = Field(default_factory=dict)


@router.post("/text")
async def upload_text(payload: TextUploadRequest) -> dict:
    # TODO: filter -> normalize JSON -> split into tree -> save to PostgreSQL -> enqueue vectorization.
    return {"status": "accepted", "source_name": payload.source_name}


@router.post("/file")
async def upload_file(file: UploadFile) -> dict:
    # TODO: detect extension, run filter pipeline, then same ingestion path as text.
    return {"status": "accepted", "filename": file.filename}
