"""SVO chunking boundary for normalized ingestion requests.

This module deliberately owns orchestration and validation only.  Chunking is
performed by the public ``SvoChunkerClient`` API and chunk values cross this
boundary through ``chunk_metadata_adapter`` factories and serializers.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias

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
    "DEFAULT_PRESET",
    "SvoChunkingBoundary",
    "SvoChunkingError",
    "SvoChunkingResult",
    "chunk_normalized_request",
)
