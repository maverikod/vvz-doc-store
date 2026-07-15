from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from chunk_metadata_adapter import BlockType, ChunkType, SemanticChunk
from svo_client import SvoChunkerClient

from doc_store_server.ingestion.source_normalizer import normalize_source
from doc_store_server.ingestion.svo_chunking import (
    DEFAULT_PRESET,
    SvoChunkingError,
    chunk_normalized_request,
)


DOCUMENT_ID = UUID("12345678-1234-4234-8234-123456789abc")
SOURCE_TEXT = "alpha beta gamma"


def _request(*, preset: str | None = None):
    result = normalize_source(raw_text=SOURCE_TEXT, document_id=DOCUMENT_ID)
    assert result.request is not None
    return replace(result.request, chunk_preset=preset or "")


def _wire_chunk(start: int, end: int, *, ordinal: int = 0) -> dict[str, object]:
    return SemanticChunk.from_dict_with_autofill_and_validation(
        {
            "uuid": str(uuid4()),
            "source_id": str(uuid4()),
            "block_id": str(uuid4()),
            "body": SOURCE_TEXT[start:end],
            "text": SOURCE_TEXT[start:end],
            "type": ChunkType.DOC_BLOCK,
            "block_type": BlockType.PARAGRAPH,
            "ordinal": ordinal,
            "block_index": ordinal,
            "start": start,
            "end": end,
        }
    ).model_dump(mode="json")


def _adapter_chunk(start: int, end: int, *, ordinal: int = 0) -> SemanticChunk:
    return SemanticChunk.from_dict_with_autofill_and_validation(
        _wire_chunk(start, end, ordinal=ordinal)
    )


def _run(awaitable):
    return asyncio.run(awaitable)


class _TransportFake:
    def __init__(self, catalog: object, chunks: object) -> None:
        self.catalog = catalog
        self.chunks = chunks
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, command: str, params: dict[str, object], **_: object) -> dict[str, object]:
        self.calls.append((command, params))
        if command == "config":
            return self.catalog  # type: ignore[return-value]
        return {
            "result": {
                "results": [{"success": True, "chunks": self.chunks}],
            }
        }


def _client(catalog: object, chunks: object) -> tuple[SvoChunkerClient, _TransportFake]:
    client = SvoChunkerClient()
    transport = _TransportFake(catalog, chunks)
    client._core = transport  # type: ignore[assignment]
    return client, transport


def test_queries_live_catalog_defaults_and_forwards_supported_presets_unchanged() -> None:
    catalog = {
        "supported_presets": [
            "technical_text",
            "scientific_text",
            "docstring",
            "plain_text",
            "org_custom",
        ]
    }
    client, transport = _client(catalog, [_wire_chunk(0, 5)])

    defaulted = _run(chunk_normalized_request(_request(), client))
    assert defaulted.contract_metadata["preset"] == DEFAULT_PRESET
    assert [name for name, _ in transport.calls] == ["config", "chunk"]
    assert transport.calls[1][1]["chunk_set"] == DEFAULT_PRESET

    for preset in ("scientific_text", "docstring", "plain_text", "org_custom"):
        transport.calls.clear()
        _run(chunk_normalized_request(_request(preset=preset), client))
        assert transport.calls[1][1]["chunk_set"] == preset


def test_unavailable_preset_has_structured_diagnostic_and_skips_chunking() -> None:
    client, transport = _client(
        {"chunk_presets": ["technical_text", "plain_text"]}, [_wire_chunk(0, 5)]
    )

    with pytest.raises(SvoChunkingError) as caught:
        _run(chunk_normalized_request(_request(preset="missing"), client))

    assert caught.value.diagnostic is not None
    assert caught.value.diagnostic.code == "UNSUPPORTED_CHUNK_PRESET"
    assert caught.value.diagnostic.context["requested"] == "missing"
    assert caught.value.diagnostic.context["supported"] == ("plain_text", "technical_text")
    assert [name for name, _ in transport.calls] == ["config"]


def test_real_client_returns_validated_serialized_ordered_chunks_and_source_ranges() -> None:
    client, transport = _client(
        {"chunk_presets": ["technical_text"]},
        [_wire_chunk(0, 5, ordinal=0), _wire_chunk(6, 10, ordinal=1)],
    )

    result = _run(chunk_normalized_request(_request(), client))

    assert all(isinstance(chunk, SemanticChunk) for chunk in result.chunks)
    assert [(chunk.start, chunk.end) for chunk in result.chunks] == [(0, 5), (6, 10)]
    assert [chunk.ordinal for chunk in result.chunks] == [0, 1]
    assert [payload["uuid"] for payload in result.serialized_chunks] == [
        str(chunk.uuid) for chunk in result.chunks
    ]
    assert result.contract_metadata["serialized_contract"] == "chunk_metadata_adapter.SemanticChunk"
    assert transport.calls[1][1]["source_id"] == str(DOCUMENT_ID)
    assert transport.calls[1][1]["chunk_only"] is True


class _PublicClientWithResult(SvoChunkerClient):
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, object]] = []

    async def capabilities(self) -> dict[str, object]:
        self.calls.append(("capabilities", None))
        return {"presets": ["technical_text"]}

    async def chunk(self, text: str, **kwargs: object) -> object:
        self.calls.append(("chunk", (text, kwargs)))
        return self.result


@pytest.mark.parametrize(
    "result",
    [
        [],
        {"foreign": True},
        ["foreign"],
        ["not-a-semantic-chunk"],
    ],
    ids=("empty", "mapping", "list-item", "string-item"),
)
def test_malformed_or_foreign_results_are_rejected(result: object) -> None:
    client = _PublicClientWithResult(result)

    with pytest.raises(SvoChunkingError):
        _run(chunk_normalized_request(_request(), client))


@pytest.mark.parametrize(
    "chunks",
    [
        [_adapter_chunk(0, 5).model_copy(update={"start": -1})],
        [_adapter_chunk(0, 5).model_copy(update={"end": len(SOURCE_TEXT) + 1})],
        [_adapter_chunk(0, 5).model_copy(update={"ordinal": 1})],
        [_adapter_chunk(0, 8, ordinal=0), _adapter_chunk(6, 10, ordinal=1)],
        [_adapter_chunk(0, 5, ordinal=1), _adapter_chunk(6, 10, ordinal=0)],
    ],
    ids=("negative-start", "out-of-bounds", "invalid-ordinal", "overlap", "out-of-order"),
)
def test_invalid_ranges_order_and_metadata_are_rejected(
    chunks: list[SemanticChunk],
) -> None:
    client = _PublicClientWithResult(chunks)

    with pytest.raises(SvoChunkingError):
        _run(chunk_normalized_request(_request(), client))


def test_no_local_splitter_or_downstream_enrichment_embedding_persistence_or_publication_runs(
) -> None:
    client, transport = _client({"presets": ["technical_text"]}, [_wire_chunk(0, 5)])

    result = _run(chunk_normalized_request(_request(), client))

    assert result.chunks[0].embedding is None
    assert result.chunks[0].block_meta == {}
    assert [name for name, _ in transport.calls] == ["config", "chunk"]
    assert all(
        name not in {"split", "window", "threshold", "embed", "save", "publish"}
        for name, _ in transport.calls
    )
