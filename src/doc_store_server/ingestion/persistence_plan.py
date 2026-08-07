"""The persistence payload named by both publication protocol implementations.

This module is a pure data declaration: it describes what a completed ingestion
has to persist, not how or where any of it is stored.  Keeping it free of every
storage dependency is what lets the mapper that builds a plan and the repository
that writes one name the same payload without importing each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PersistencePlan:
    """One document version's fully resolved rows, ready to be written."""

    document_id: UUID
    source_version_id: str
    normalized_source_version_id: str
    operation_id: str
    command: str
    text_value: str
    filename: str | None
    content_sha256: str
    chunking_strategy: str
    source_version: int
    title: str
    source_name: str | None
    length: int
    body_sha256: str
    file_id: UUID
    doc_meta: Mapping[str, Any]
    # Facts about the *original* bytes, retained so the immutable PrimarySource
    # can be stated from what was actually received rather than guessed. Both are
    # unknown on the rechunk path, where the File already names its original.
    media_type: str | None = None
    byte_length: int | None = None


__all__ = ("PersistencePlan",)
