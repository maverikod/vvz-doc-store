"""Separated JSONL logs for installed ingestion runtime events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from doc_store_server.runtime.previews import chunk_preview


DEFAULT_LOG_DIR = "/var/log/doc-store"


def log_error_event(payload: Mapping[str, Any]) -> None:
    """Append one structured error event without interrupting ingestion."""

    _append("errors.jsonl", payload)


def log_processed_file_event(payload: Mapping[str, Any]) -> None:
    """Append one structured processed-file event."""

    _append("processed_files.jsonl", payload)


def log_processed_text_event(payload: Mapping[str, Any]) -> None:
    """Append one structured processed-text preview event."""

    _append("processed_texts.jsonl", payload)


def preview_chars() -> int:
    """Return configured processed-text preview length."""

    from doc_store_server.runtime.previews import preview_chars as _preview_chars

    return _preview_chars("DOC_STORE_TEXT_LOG_PREVIEW_CHARS")


def text_preview(value: str) -> str:
    """Return the first configured characters of text for preview logging."""

    return chunk_preview(value, limit=preview_chars())


def _append(filename: str, payload: Mapping[str, Any]) -> None:
    try:
        import os

        directory = Path(os.getenv("DOC_STORE_EVENT_LOG_DIR", DEFAULT_LOG_DIR))
        directory.mkdir(parents=True, exist_ok=True)
        event = {"timestamp": _now(), **dict(payload)}
        with (directory / filename).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "log_error_event",
    "log_processed_file_event",
    "log_processed_text_event",
    "preview_chars",
    "text_preview",
]
