"""HTTP client for doc-store server API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class DocStoreClient:
    """Small API client used by applications and file watcher."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    async def upload_text(
        self,
        text: str,
        *,
        source_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/documents/text",
                headers=self._headers(),
                json={"text": text, "source_name": source_name, "metadata": metadata or {}},
            )
            response.raise_for_status()
            return response.json()

    async def upload_file(
        self,
        path: str | Path,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        file_path = Path(path)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            with file_path.open("rb") as stream:
                response = await client.post(
                    f"{self.base_url}/api/v1/documents/file",
                    headers=self._headers(),
                    files={"file": (file_path.name, stream)},
                    data={"metadata": metadata or {}},
                )
            response.raise_for_status()
            return response.json()
