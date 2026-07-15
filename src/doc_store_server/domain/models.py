"""Canonical in-memory document hierarchy models."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4


def _validate_uuid4(value: UUID, field_name: str) -> None:
    """Reject identifiers that are not UUID version 4 values."""
    if not isinstance(value, UUID) or value.version != 4:
        raise ValueError(f"{field_name} must be a UUID version 4 value")


@dataclass
class Document:
    """A document containing chapters in their canonical order."""

    id: UUID = field(default_factory=uuid4)
    chapters: list[Chapter] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_uuid4(self.id, "Document.id")


@dataclass
class Chapter:
    """A chapter belonging to one document and containing ordered paragraphs."""

    document: Document = field(compare=False, repr=False)
    id: UUID = field(default_factory=uuid4)
    paragraphs: list[Paragraph] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_uuid4(self.id, "Chapter.id")


@dataclass
class Paragraph:
    """A paragraph belonging to one chapter."""

    chapter: Chapter = field(compare=False, repr=False)
    id: UUID = field(default_factory=uuid4)
    text: str = ""

    def __post_init__(self) -> None:
        _validate_uuid4(self.id, "Paragraph.id")


__all__ = ("Chapter", "Document", "Paragraph")
