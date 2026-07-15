"""Typed source normalization boundary for document ingestion."""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID, uuid5, NAMESPACE_URL


DEFAULT_MAX_SOURCE_BYTES = 16 * 1024 * 1024
DEFAULT_PRESET = "technical_text"


class TransferDescriptor(Protocol):
    """Public adapter-like file transfer shape consumed by this boundary."""

    filename: str | None
    media_type: str | None

    def read(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class NormalizationDiagnostic:
    """Structured rejection or filter failure diagnostic."""

    code: str
    message: str
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Metadata retained from the accepted source without making it authoritative."""

    kind: str
    filename: str | None = None
    media_type: str | None = None
    byte_length: int = 0
    content_sha256: str = ""


@dataclass(frozen=True, slots=True)
class NormalizedIngestionRequest:
    """Immutable request passed to the SVO chunking boundary."""

    document_id: UUID
    source_version_id: str
    text: str
    source_metadata: SourceMetadata
    selected_filter: str
    normalization_profile: str
    chunk_preset: str


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Success-or-diagnostic result for source normalization."""

    request: NormalizedIngestionRequest | None = None
    diagnostic: NormalizationDiagnostic | None = None

    @property
    def ok(self) -> bool:
        return self.request is not None and self.diagnostic is None


FilterFunction = Callable[[bytes, SourceMetadata], str]


@dataclass(frozen=True, slots=True)
class FormatFilter:
    """Existing filter contract as consumed by the normalizer."""

    name: str
    media_types: frozenset[str] = frozenset()
    extensions: frozenset[str] = frozenset()
    apply: FilterFunction | None = None

    def filter(self, payload: bytes, metadata: SourceMetadata) -> str:
        if self.apply is None:
            return payload.decode("utf-8")
        return self.apply(payload, metadata)


TEXT_FILTER = FormatFilter(
    name="plain_text",
    media_types=frozenset({"text/plain"}),
    extensions=frozenset({".txt", ".text", ".md", ".markdown"}),
)


def normalize_source(
    *,
    raw_text: str | None = None,
    transferred_file: TransferDescriptor | Mapping[str, Any] | None = None,
    document_id: UUID | str | None = None,
    trusted_format_hint: str | None = None,
    media_type: str | None = None,
    filename: str | None = None,
    normalization_profile: str = "default",
    chunk_preset: str | None = None,
    filters: Mapping[str, FormatFilter] | None = None,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
) -> NormalizationResult:
    """Normalize exactly one accepted source into an immutable ingestion request."""

    if (raw_text is None) == (transferred_file is None):
        return _reject(
            "INVALID_SOURCE_COUNT",
            "provide exactly one of raw_text or transferred_file",
            raw_text=raw_text is not None,
            transferred_file=transferred_file is not None,
        )
    if max_source_bytes <= 0:
        return _reject("INVALID_LIMIT", "max_source_bytes must be positive")

    filter_map = dict(filters or {"plain_text": TEXT_FILTER})
    if not filter_map:
        return _reject("NO_FILTERS", "at least one supported format filter is required")

    if raw_text is not None:
        source_bytes = raw_text.encode("utf-8")
        metadata = SourceMetadata(
            kind="raw_text",
            filename=filename,
            media_type=media_type or "text/plain",
            byte_length=len(source_bytes),
            content_sha256=_sha256(source_bytes),
        )
    else:
        file_payload = _read_transferred_file(transferred_file)
        if isinstance(file_payload, NormalizationDiagnostic):
            return NormalizationResult(diagnostic=file_payload)
        payload_bytes, file_name, file_media_type = file_payload
        source_bytes = payload_bytes
        metadata = SourceMetadata(
            kind="transferred_file",
            filename=filename or file_name,
            media_type=media_type or file_media_type or _guess_media_type(filename or file_name),
            byte_length=len(source_bytes),
            content_sha256=_sha256(source_bytes),
        )

    if not source_bytes:
        return _reject("EMPTY_SOURCE", "source content is empty")
    if len(source_bytes) > max_source_bytes:
        return _reject(
            "SOURCE_TOO_LARGE",
            "source content exceeds max_source_bytes",
            byte_length=len(source_bytes),
            max_source_bytes=max_source_bytes,
        )

    selected = _select_filter(
        filter_map,
        trusted_format_hint=trusted_format_hint,
        media_type=metadata.media_type,
        filename=metadata.filename,
    )
    if isinstance(selected, NormalizationDiagnostic):
        return NormalizationResult(diagnostic=selected)

    try:
        text = selected.filter(source_bytes, metadata)
    except Exception as exc:  # pragma: no cover - exact filter errors are external.
        return _reject(
            "FILTER_FAILED",
            "format filter failed",
            filter=selected.name,
            error=type(exc).__name__,
            detail=str(exc),
        )
    if not text.strip():
        return _reject("EMPTY_NORMALIZED_TEXT", "normalized text is empty", filter=selected.name)

    document_uuid = _document_uuid(document_id, metadata)
    source_version_id = _source_version_id(
        document_uuid=document_uuid,
        source_hash=metadata.content_sha256,
        profile=normalization_profile,
        filter_name=selected.name,
        chunk_preset=chunk_preset or DEFAULT_PRESET,
    )
    return NormalizationResult(
        request=NormalizedIngestionRequest(
            document_id=document_uuid,
            source_version_id=source_version_id,
            text=text,
            source_metadata=metadata,
            selected_filter=selected.name,
            normalization_profile=normalization_profile,
            chunk_preset=chunk_preset or DEFAULT_PRESET,
        )
    )


def _read_transferred_file(
    transferred_file: TransferDescriptor | Mapping[str, Any] | None,
) -> tuple[bytes, str | None, str | None] | NormalizationDiagnostic:
    if transferred_file is None:
        return NormalizationDiagnostic("MISSING_TRANSFER", "transferred file is missing")
    if isinstance(transferred_file, Mapping):
        payload = transferred_file.get("content", transferred_file.get("data"))
        filename = _optional_str(transferred_file.get("filename") or transferred_file.get("name"))
        media_type = _optional_str(
            transferred_file.get("media_type") or transferred_file.get("content_type")
        )
    else:
        payload = transferred_file.read()
        filename = _optional_str(getattr(transferred_file, "filename", None))
        media_type = _optional_str(getattr(transferred_file, "media_type", None))
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(payload, bytes):
        return NormalizationDiagnostic(
            "INVALID_TRANSFER_PAYLOAD",
            "transferred file payload must be bytes or text",
            MappingProxyType({"payload_type": type(payload).__name__}),
        )
    return payload, filename, media_type


def _select_filter(
    filters: Mapping[str, FormatFilter],
    *,
    trusted_format_hint: str | None,
    media_type: str | None,
    filename: str | None,
) -> FormatFilter | NormalizationDiagnostic:
    hinted = filters.get(trusted_format_hint or "") if trusted_format_hint else None
    if trusted_format_hint and hinted is None:
        return NormalizationDiagnostic(
            "UNSUPPORTED_FORMAT_HINT",
            "trusted format hint is not supported",
            MappingProxyType({"hint": trusted_format_hint, "supported": sorted(filters)}),
        )
    media_matches = _filters_for_media(filters, media_type)
    extension_matches = _filters_for_extension(filters, filename)
    candidates = [item for item in (hinted, *media_matches, *extension_matches) if item is not None]
    if not candidates:
        return NormalizationDiagnostic(
            "UNSUPPORTED_SOURCE_FORMAT",
            "no supported format filter matched source metadata",
            MappingProxyType({"media_type": media_type, "filename": filename}),
        )
    names = {candidate.name for candidate in candidates}
    if len(names) > 1:
        return NormalizationDiagnostic(
            "CONFLICTING_FORMAT_FILTERS",
            "source metadata selects conflicting format filters",
            MappingProxyType({"filters": sorted(names)}),
        )
    return candidates[0]


def _filters_for_media(filters: Mapping[str, FormatFilter], media_type: str | None) -> list[FormatFilter]:
    if not media_type:
        return []
    normalized = media_type.lower()
    return [item for item in filters.values() if normalized in item.media_types]


def _filters_for_extension(filters: Mapping[str, FormatFilter], filename: str | None) -> list[FormatFilter]:
    if not filename or "." not in filename:
        return []
    extension = "." + filename.rsplit(".", 1)[-1].lower()
    return [item for item in filters.values() if extension in item.extensions]


def _document_uuid(document_id: UUID | str | None, metadata: SourceMetadata) -> UUID:
    if isinstance(document_id, UUID):
        return document_id
    if isinstance(document_id, str) and document_id.strip():
        try:
            return UUID(document_id)
        except ValueError:
            return uuid5(NAMESPACE_URL, f"doc-store:document:{document_id}")
    stable_name = metadata.filename or metadata.content_sha256
    return uuid5(NAMESPACE_URL, f"doc-store:document:{stable_name}")


def _source_version_id(
    *,
    document_uuid: UUID,
    source_hash: str,
    profile: str,
    filter_name: str,
    chunk_preset: str,
) -> str:
    payload = "|".join((str(document_uuid), source_hash, profile, filter_name, chunk_preset))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _guess_media_type(filename: str | None) -> str | None:
    if not filename:
        return None
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _reject(code: str, message: str, **context: Any) -> NormalizationResult:
    return NormalizationResult(
        diagnostic=NormalizationDiagnostic(code=code, message=message, context=MappingProxyType(context))
    )


__all__ = [
    "DEFAULT_PRESET",
    "FormatFilter",
    "NormalizationDiagnostic",
    "NormalizationResult",
    "NormalizedIngestionRequest",
    "SourceMetadata",
    "TEXT_FILTER",
    "TransferDescriptor",
    "normalize_source",
]
