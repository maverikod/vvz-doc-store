"""Shared text preview helpers for search results and runtime logs."""

from __future__ import annotations

import os


DEFAULT_PREVIEW_CHARS = 220


def preview_chars(env_name: str = "DOC_STORE_CHUNK_PREVIEW_CHARS") -> int:
    raw = os.getenv(env_name)
    if raw is None:
        return DEFAULT_PREVIEW_CHARS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_PREVIEW_CHARS


def chunk_preview(value: str, *, limit: int | None = None) -> str:
    effective_limit = preview_chars() if limit is None else max(0, int(limit))
    if not effective_limit:
        return ""
    return " ".join(str(value).split())[:effective_limit]


__all__ = ["chunk_preview", "preview_chars"]
