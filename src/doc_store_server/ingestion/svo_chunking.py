"""SVO chunking boundary for normalized ingestion requests.

This module deliberately owns orchestration and validation only.  Chunking is
performed by the public ``SvoChunkerClient`` API and chunk values cross this
boundary through ``chunk_metadata_adapter`` factories and serializers.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias
from uuid import UUID

from chunk_metadata_adapter import SemanticChunk

from doc_store_server.domain.semantic_chunk import (
    deserialize_semantic_chunk,
    serialize_semantic_chunk,
)
from doc_store_server.ingestion.source_normalizer import NormalizedIngestionRequest


DEFAULT_PRESET = "technical_text"
SerializedChunk: TypeAlias = Mapping[str, Any]


class PublicSvoChunker(Protocol):
    """The public client surface required by this boundary."""

    async def chunk(self, text: str, **kwargs: Any) -> Sequence[SemanticChunk]: ...


class SvoChunkingError(ValueError):
    """Raised when capabilities or a chunker result violates the contract."""

    def __init__(self, message: str, *, diagnostic: "ChunkingDiagnostic | None" = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


@dataclass(frozen=True, slots=True)
class ChunkingDiagnostic:
    code: str
    message: str
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SvoChunkingResult:
    """Immutable chunks plus metadata consumed by later hierarchy stages."""

    chunks: tuple[SemanticChunk, ...]
    serialized_chunks: tuple[SerializedChunk, ...]
    contract_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeChunk:
    """One validated chunk returned by the external chunking service."""

    uuid: UUID
    text: str
    start: int
    end: int
    ordinal: int
    metadata: Mapping[str, Any]


class ChunkerError(RuntimeError):
    """Domain error raised by the runtime chunker wrapper."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class SvoRuntimeChunker:
    """Thin runtime wrapper over the public SvoChunkerClient."""

    def __init__(
        self,
        client: PublicSvoChunker,
        *,
        chunk_sets: Mapping[str, str | None] | None = None,
        language: str | None = None,
        project: str | None = None,
    ) -> None:
        self._client = client
        self._chunk_sets = dict(chunk_sets or {})
        self._language = language
        self._project = project

    async def chunk(
        self,
        *,
        text: str,
        strategy: str,
        source_id: str,
    ) -> tuple[RuntimeChunk, ...]:
        params = _strategy_params(strategy)
        chunk_set = self._chunk_sets.get(strategy)
        if chunk_set:
            params["chunk_set"] = chunk_set
        if self._language:
            params["language"] = self._language
        if self._project:
            params["project"] = self._project
        try:
            raw_chunks = await self._client.chunk(
                text,
                source_id=source_id,
                chunk_only=True,
                chunk_type="DocBlock",
                chunking_version="1.0",
                **params,
            )
        except ChunkerError:
            raise
        except (TimeoutError, OSError) as exc:
            raise ChunkerError(
                "CHUNKER_UNAVAILABLE",
                "external chunker is unavailable",
                {"error_type": type(exc).__name__, "message": str(exc)},
            ) from exc
        except Exception as exc:
            if _looks_like_connection_error(exc):
                raise ChunkerError(
                    "CHUNKER_UNAVAILABLE",
                    "external chunker is unavailable",
                    {"error_type": type(exc).__name__, "message": str(exc)},
                ) from exc
            raise ChunkerError(
                "CHUNKER_INTERNAL_ERROR",
                "external chunker failed",
                {"error_type": type(exc).__name__, "message": str(exc)},
            ) from exc
        return _validate_runtime_chunks(raw_chunks, text, source_id)


def _strategy_params(strategy: str) -> dict[str, Any]:
    if strategy == "paragraph":
        return {"use_sv": False, "aggregation_mode": "paragraph"}
    if strategy == "sentence":
        return {"use_sv": False, "aggregation_mode": "sentence"}
    if strategy == "semantic":
        return {"use_sv": False, "aggregation_mode": "sentence", "chunk_set": DEFAULT_PRESET}
    raise ChunkerError(
        "CHUNKER_UNSUPPORTED_STRATEGY",
        "chunking strategy is not supported by the SVO runtime wrapper",
        {"strategy": strategy},
    )


def _looks_like_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "timeout" in name or "connection" in name or "connect" in name


def _validate_runtime_chunks(
    raw_chunks: Any,
    source_text: str,
    source_id: str,
) -> tuple[RuntimeChunk, ...]:
    if isinstance(raw_chunks, (str, bytes, bytearray)) or not isinstance(raw_chunks, Sequence):
        raise ChunkerError(
            "CHUNKER_INVALID_RESPONSE",
            "external chunker returned a non-list response",
            {"response_type": type(raw_chunks).__name__},
        )
    if not raw_chunks:
        raise ChunkerError("CHUNKER_EMPTY_RESULT", "external chunker returned no chunks")
    expected_source = str(UUID(source_id))
    previous_end = 0
    result: list[RuntimeChunk] = []
    for index, raw in enumerate(raw_chunks):
        if not isinstance(raw, SemanticChunk):
            raise ChunkerError(
                "CHUNKER_INVALID_RESPONSE",
                "external chunker returned a non-SemanticChunk item",
                {"index": index, "item_type": type(raw).__name__},
            )
        try:
            payload = serialize_semantic_chunk(raw)
            chunk = deserialize_semantic_chunk(payload)
        except Exception as exc:
            raise ChunkerError(
                "CHUNKER_INVALID_RESPONSE",
                "external chunker returned an invalid SemanticChunk",
                {"index": index, "error_type": type(exc).__name__, "message": str(exc)},
            ) from exc
        raw_start, raw_end = chunk.start, chunk.end
        raw_body = str(getattr(chunk, "body", None) or "")
        body = str(getattr(chunk, "text", None) or raw_body or "")
        if chunk.source_id != expected_source:
            raise ChunkerError(
                "CHUNKER_CONTRACT_ERROR",
                "external chunker returned a chunk for a different source_id",
                {"index": index, "expected": expected_source, "actual": chunk.source_id},
            )
        if not isinstance(raw_start, int) or not isinstance(raw_end, int):
            raise ChunkerError(
                "CHUNKER_CONTRACT_ERROR",
                "external chunker returned a chunk without integer source range",
                {"index": index},
            )
        if not body:
            raise ChunkerError(
                "CHUNKER_CONTRACT_ERROR",
                "external chunker returned an empty chunk body",
                {"index": index},
            )
        start, end = _resolve_runtime_range(
            source_text=source_text,
            previous_end=previous_end,
            raw_start=raw_start,
            raw_end=raw_end,
            raw_body=raw_body,
            text_body=body,
        )
        if start is None or end is None:
            raise ChunkerError(
                "CHUNKER_CONTRACT_ERROR",
                "external chunker returned a chunk that cannot be located in the source text",
                {
                    "index": index,
                    "start": raw_start,
                    "end": raw_end,
                    "previous_end": previous_end,
                    "source_length": len(source_text),
                },
            )
        if start < previous_end:
            raise ChunkerError(
                "CHUNKER_CONTRACT_ERROR",
                "external chunker returned overlapping or unordered ranges",
                {"index": index, "start": start, "previous_end": previous_end},
            )
        if chunk.ordinal is not None and chunk.ordinal != index:
            raise ChunkerError(
                "CHUNKER_CONTRACT_ERROR",
                "external chunker returned non-sequential ordinals",
                {"index": index, "ordinal": chunk.ordinal},
            )
        chunk_uuid = UUID(str(chunk.uuid))
        if chunk_uuid.version != 4:
            raise ChunkerError(
                "CHUNKER_CONTRACT_ERROR",
                "external chunker returned a non-UUID4 chunk uuid",
                {"index": index, "uuid": str(chunk.uuid)},
            )
        result.append(
            RuntimeChunk(
                uuid=chunk_uuid,
                text=body,
                start=start,
                end=end,
                ordinal=index,
                metadata=MappingProxyType(payload),
            )
        )
        previous_end = end
    return tuple(result)


def _resolve_runtime_range(
    *,
    source_text: str,
    previous_end: int,
    raw_start: int,
    raw_end: int,
    raw_body: str,
    text_body: str,
) -> tuple[int | None, int | None]:
    if (
        0 <= raw_start < raw_end <= len(source_text)
        and raw_start >= previous_end
        and source_text[raw_start:raw_end] in {raw_body, text_body}
    ):
        return raw_start, raw_end

    for candidate in _range_candidates(raw_body, text_body):
        found = source_text.find(candidate, previous_end)
        if found >= 0:
            return found, found + len(candidate)

    stripped = text_body.strip()
    if stripped:
        pattern = r"\s+".join(re.escape(part) for part in stripped.split())
        match = re.search(pattern, source_text[previous_end:])
        if match:
            start = previous_end + match.start()
            return start, previous_end + match.end()
    return None, None


def _range_candidates(raw_body: str, text_body: str) -> tuple[str, ...]:
    values: list[str] = []
    for candidate in (raw_body, text_body, raw_body.strip(), text_body.strip()):
        if candidate and candidate not in values:
            values.append(candidate)
    return tuple(values)


class SvoChunkingBoundary:
    """Adapt one normalized request to validated, ordered public chunks."""

    def __init__(self, client: PublicSvoChunker) -> None:
        self._client = client

    async def chunk(
        self,
        request: NormalizedIngestionRequest,
        *,
        preset: str | None = None,
    ) -> SvoChunkingResult:
        supported = await _query_supported_presets(self._client)
        requested = preset if preset is not None else _request_preset(request)
        selected = requested or DEFAULT_PRESET
        if selected not in supported:
            diagnostic = ChunkingDiagnostic(
                code="UNSUPPORTED_CHUNK_PRESET",
                message="requested chunk preset is not available on the SVO server",
                context=MappingProxyType(
                    {"requested": selected, "supported": tuple(sorted(supported))}
                ),
            )
            raise SvoChunkingError(diagnostic.message, diagnostic=diagnostic)

        # ``chunk`` is the only chunking operation used here.  In particular,
        # this boundary never reproduces the server's windows or split logic.
        raw_chunks = await self._client.chunk(
            request.text,
            chunk_set=selected,
            source_id=str(request.document_id),
            chunk_only=True,
        )
        chunks, serialized = _validate_result(raw_chunks, request.text)
        metadata = MappingProxyType(
            {
                "preset": selected,
                "chunking_version": "1.0",
                "source_version_id": request.source_version_id,
                "chunk_count": len(chunks),
                "serialized_contract": "chunk_metadata_adapter.SemanticChunk",
            }
        )
        return SvoChunkingResult(chunks, serialized, metadata)


async def chunk_normalized_request(
    request: NormalizedIngestionRequest,
    client: PublicSvoChunker,
    *,
    preset: str | None = None,
) -> SvoChunkingResult:
    """Chunk a normalized request through the public SVO client contract."""

    return await SvoChunkingBoundary(client).chunk(request, preset=preset)


async def _query_supported_presets(client: Any) -> frozenset[str]:
    """Read the server catalog using only public capability/documentation APIs."""

    for method_name in ("capabilities", "get_capabilities", "config", "info", "help"):
        method = getattr(client, method_name, None)
        if not callable(method):
            continue
        value = method()
        if inspect.isawaitable(value):
            value = await value
        presets = _extract_presets(value)
        if presets:
            return frozenset(presets)
    raise SvoChunkingError(
        "SVO server capability catalog is unavailable",
        diagnostic=ChunkingDiagnostic(
            code="PRESET_CATALOG_UNAVAILABLE",
            message="SVO server did not expose a supported preset catalog",
        ),
    )


def _extract_presets(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        found: set[str] = set()
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if normalized_key in {
                "chunk_sets",
                "chunk_presets",
                "presets",
                "supported_presets",
                "supported_chunk_sets",
            }:
                found.update(_preset_values(item))
            elif isinstance(item, (Mapping, list, tuple, set, frozenset)):
                found.update(_extract_presets(item))
        return found
    if isinstance(value, (list, tuple, set, frozenset)):
        return {item for item in value if isinstance(item, str) and item.strip()}
    return set()


def _preset_values(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {str(key) for key in value if str(key).strip()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {item for item in value if isinstance(item, str) and item.strip()}
    return set()


def _request_preset(request: NormalizedIngestionRequest) -> str | None:
    value = getattr(request, "chunk_preset", None)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SvoChunkingError("chunk preset must be a string or None")
    value = value.strip()
    return value or None


def _validate_result(
    raw_chunks: Any,
    source_text: str,
) -> tuple[tuple[SemanticChunk, ...], tuple[SerializedChunk, ...]]:
    if isinstance(raw_chunks, (str, bytes, bytearray)) or not isinstance(raw_chunks, Sequence):
        raise SvoChunkingError("SVO returned a foreign chunk collection shape")
    if not raw_chunks:
        raise SvoChunkingError("SVO returned an empty chunk collection")

    validated: list[SemanticChunk] = []
    serialized: list[SerializedChunk] = []
    previous_end = 0
    for index, raw in enumerate(raw_chunks):
        if not isinstance(raw, SemanticChunk):
            raise SvoChunkingError(f"SVO result item {index} is not a SemanticChunk")
        try:
            payload = serialize_semantic_chunk(raw)
            chunk = deserialize_semantic_chunk(payload)
        except Exception as exc:
            raise SvoChunkingError(f"SVO result item {index} failed adapter validation") from exc
        start, end = chunk.start, chunk.end
        if not isinstance(start, int) or not isinstance(end, int):
            raise SvoChunkingError(f"SVO result item {index} has invalid source range")
        if start < 0 or end <= start or end > len(source_text):
            raise SvoChunkingError(f"SVO result item {index} has an out-of-bounds source range")
        if start < previous_end:
            raise SvoChunkingError("SVO result ranges are overlapping or out of order")
        if chunk.ordinal is not None and chunk.ordinal != index:
            raise SvoChunkingError("SVO result ordinals do not preserve returned order")
        validated.append(chunk)
        serialized.append(MappingProxyType(payload))
        previous_end = end
    return tuple(validated), tuple(serialized)


__all__ = (
    "ChunkingDiagnostic",
    "ChunkerError",
    "DEFAULT_PRESET",
    "RuntimeChunk",
    "SvoChunkingBoundary",
    "SvoChunkingError",
    "SvoChunkingResult",
    "SvoRuntimeChunker",
    "chunk_normalized_request",
)
