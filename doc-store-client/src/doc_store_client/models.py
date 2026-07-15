"""Stable, transport-neutral public contracts for ``doc-store-client``.

The models intentionally contain validation and payload conversion only.  They
do not know how a command is sent, queued, retried, persisted, or transferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Mapping, Self

from chunk_metadata_adapter import ChunkQuery


Payload = Mapping[str, Any]


def _payload(model: Any, *, omit_none: bool = True) -> dict[str, Any]:
    """Return fields in declaration order using their public server names."""

    result: dict[str, Any] = {}
    for model_field in fields(model):
        value = getattr(model, model_field.name)
        if omit_none and value is None:
            continue
        if isinstance(value, tuple):
            value = list(value)
        result[model_field.name] = value
    return result


def _read(cls: type[Any], payload: Payload) -> Any:
    """Build a model while rejecting unknown server fields deterministically."""

    known = {model_field.name for model_field in fields(cls)}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ValueError(f"unknown {cls.__name__} fields: {', '.join(unknown)}")
    return cls(
        **{
            model_field.name: payload[model_field.name]
            for model_field in fields(cls)
            if model_field.name in payload
        }
    )


class PublicModel:
    """Small common API shared by all public payload models."""

    _omit_none: ClassVar[bool] = True

    def to_params(self) -> dict[str, Any]:
        return _payload(self, omit_none=self._omit_none)

    def to_payload(self) -> dict[str, Any]:
        return self.to_params()

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        if not isinstance(payload, Mapping):
            raise TypeError(f"{cls.__name__} payload must be an object")
        return _read(cls, payload)


@dataclass(frozen=True, kw_only=True)
class DocumentWriteRequest(PublicModel):
    """Shared source contract for document create and update."""

    document_id: str
    source_version_id: str
    raw_text: str | None = None
    transferred_file: Mapping[str, Any] | None = None
    chunking_strategy: str | None = None

    def __post_init__(self) -> None:
        if not self.document_id.strip() or not self.source_version_id.strip():
            raise ValueError("document_id and source_version_id must be non-empty")
        if self.raw_text is not None and self.transferred_file is not None:
            raise ValueError("raw_text and transferred_file are mutually exclusive")
        if self.raw_text is not None and not self.raw_text:
            raise ValueError("raw_text must be non-empty")
        if self.chunking_strategy is not None and self.chunking_strategy not in {
            "paragraph",
            "sentence",
            "semantic",
        }:
            raise ValueError("chunking_strategy must be paragraph, sentence, or semantic")


@dataclass(frozen=True, kw_only=True)
class DocumentCreateRequest(DocumentWriteRequest):
    """Request payload for ``document_create``."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.chunking_strategy is None:
            raise ValueError("chunking_strategy is required")


@dataclass(frozen=True, kw_only=True)
class DocumentUpdateRequest(DocumentWriteRequest):
    """Request payload for ``document_update``."""


@dataclass(frozen=True, kw_only=True)
class DocumentWriteResult(PublicModel):
    """Stable result returned by document create/update operations."""

    status: str
    operation_id: str
    document_id: str
    source_version_id: str
    details: Mapping[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        known = {model_field.name for model_field in fields(cls)}
        extras = {key: values.pop(key) for key in sorted(set(values) - known)}
        if extras:
            details = dict(values.get("details") or {})
            details.update(extras)
            values["details"] = details
        return _read(cls, values)


DocumentCreateResult = DocumentWriteResult
DocumentUpdateResult = DocumentWriteResult


@dataclass(frozen=True, kw_only=True)
class DocumentChunkRequest(PublicModel):
    document_id: str
    chunking_strategy: str | None = None

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("document_id must be non-empty")
        if self.chunking_strategy is not None and self.chunking_strategy not in {
            "paragraph",
            "sentence",
            "semantic",
        }:
            raise ValueError("chunking_strategy must be paragraph, sentence, or semantic")


DocumentChunkResult = DocumentWriteResult


@dataclass(frozen=True, kw_only=True)
class DocumentRebindRequest(PublicModel):
    document_id: str
    project: str | None = None
    document_properties: Mapping[str, Any] | None = None
    chunk_properties: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("document_id must be non-empty")
        if self.project is not None and not self.project.strip():
            raise ValueError("project must be non-empty when supplied")
        if not any(
            value is not None
            for value in (self.project, self.document_properties, self.chunk_properties)
        ):
            raise ValueError("at least one rebind field is required")


@dataclass(frozen=True, kw_only=True)
class DocumentRebindResult(PublicModel):
    outcome: str
    document_id: str
    project: str | None = None
    document_properties: Mapping[str, Any] | None = None
    chunk_properties: Mapping[str, Any] | None = None
    updated: Mapping[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class DocumentDeleteRequest(PublicModel):
    document_id: str
    version_token: str

    def __post_init__(self) -> None:
        if not self.document_id.strip() or not self.version_token.strip():
            raise ValueError("document_id and version_token must be non-empty")


@dataclass(frozen=True, kw_only=True)
class DocumentDeleteResult(PublicModel):
    outcome: str
    document_id: str


@dataclass(frozen=True, kw_only=True)
class ProcessingStatusRequest(PublicModel):
    operation_id: str
    document_id: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_id.strip() or (
            self.document_id is not None and not self.document_id.strip()
        ):
            raise ValueError("operation_id and document_id must be non-empty when supplied")


@dataclass(frozen=True, kw_only=True)
class ServerError(PublicModel):
    """Structured error data returned by a server command."""

    code: str
    message: str
    details: Mapping[str, Any] | None = None
    type: str | None = None

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        known = {model_field.name for model_field in fields(cls)}
        extras = {key: values.pop(key) for key in sorted(set(values) - known)}
        if extras:
            details = dict(values.get("details") or {})
            details.update(extras)
            values["details"] = details
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class ProcessingStatusResult(PublicModel):
    operation_id: str
    status: str
    progress: int | float | None = None
    timestamps: Mapping[str, Any] = field(default_factory=dict)
    document_reference: str | None = None
    version_reference: str | None = None
    failure: ServerError | Mapping[str, Any] | None = None
    document_id: str | None = None
    requested_document_id: str | None = None

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        if isinstance(self.failure, ServerError):
            result["failure"] = self.failure.to_payload()
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        failure = values.get("failure")
        if isinstance(failure, Mapping):
            values["failure"] = ServerError.from_payload(failure)
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class RetrievalRequest(PublicModel):
    source_version: int | None = None

    def __post_init__(self) -> None:
        if self.source_version is not None and (
            isinstance(self.source_version, bool) or self.source_version <= 0
        ):
            raise ValueError("source_version must be a positive integer")


@dataclass(frozen=True, kw_only=True)
class DocumentGetRequest(RetrievalRequest):
    document_id: str


@dataclass(frozen=True, kw_only=True)
class ChapterGetRequest(RetrievalRequest):
    chapter_id: str


@dataclass(frozen=True, kw_only=True)
class ParagraphGetRequest(RetrievalRequest):
    paragraph_id: str


@dataclass(frozen=True, kw_only=True)
class RetrievalResult(PublicModel):
    entity: str
    identifier: str
    source_version: int | None = None
    value: Any = None


DocumentGetResult = RetrievalResult
ChapterGetResult = RetrievalResult
ParagraphGetResult = RetrievalResult


@dataclass(frozen=True, kw_only=True)
class RankedSearchHit(PublicModel):
    chunk_id: str
    chunk: Mapping[str, Any]
    bm25_score: float | None = None
    semantic_score: float | None = None
    hybrid_score: float | None = None
    rank: int = 0
    matched_fields: tuple[str, ...] | None = None
    highlights: Mapping[str, tuple[str, ...]] | None = None
    search_metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("bm25_score", "semantic_score", "hybrid_score"):
            score = getattr(self, name)
            if score is not None and not 0 <= score <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.rank < 0:
            raise ValueError("rank must be non-negative")


@dataclass(frozen=True, kw_only=True)
class SearchResult(PublicModel):
    status: str
    results: tuple[RankedSearchHit, ...] = ()
    total_results: int | None = None
    search_time: float | None = None
    query_time: float | None = None
    metadata: Mapping[str, Any] | None = None
    error: ServerError | Mapping[str, Any] | None = None

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        result["results"] = [hit.to_payload() for hit in self.results]
        if isinstance(self.error, ServerError):
            result["error"] = self.error.to_payload()
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        nested = values.pop("data", None)
        if isinstance(nested, Mapping):
            values = {**dict(nested), **values}
        values.setdefault("status", "success")
        values["results"] = tuple(
            RankedSearchHit.from_payload(item) for item in values.get("results", ())
        )
        if isinstance(values.get("error"), Mapping):
            values["error"] = ServerError.from_payload(values["error"])
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class OperationState(PublicModel):
    operation_id: str
    status: str
    document_id: str | None = None
    source_version_id: str | None = None
    message: str | None = None
    error: ServerError | Mapping[str, Any] | None = None

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        if isinstance(self.error, ServerError):
            result["error"] = self.error.to_payload()
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        if isinstance(values.get("error"), Mapping):
            values["error"] = ServerError.from_payload(values["error"])
        return _read(cls, values)


__all__ = [
    "ChapterGetRequest", "ChapterGetResult", "ChunkQuery", "DocumentCreateRequest",
    "DocumentCreateResult", "DocumentChunkRequest", "DocumentChunkResult",
    "DocumentDeleteRequest", "DocumentDeleteResult", "DocumentGetRequest", "DocumentGetResult",
    "DocumentRebindRequest", "DocumentRebindResult", "DocumentUpdateRequest", "DocumentUpdateResult",
    "DocumentWriteRequest", "DocumentWriteResult", "OperationState", "ParagraphGetRequest",
    "ParagraphGetResult", "ProcessingStatusRequest", "ProcessingStatusResult", "RankedSearchHit",
    "RetrievalRequest", "RetrievalResult", "SearchResult", "ServerError",
]
