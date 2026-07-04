"""Canonical text tree models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SentenceNode(BaseModel):
    text: str
    language: str | None = None


class ParagraphNode(BaseModel):
    level: int = 0
    title: str | None = None
    body: str
    sentences: list[SentenceNode] = Field(default_factory=list)


class ChapterNode(BaseModel):
    title: str | None = None
    paragraphs: list[ParagraphNode] = Field(default_factory=list)


class ProjectNode(BaseModel):
    title: str
    source_name: str
    chapters: list[ChapterNode] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
