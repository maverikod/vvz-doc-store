from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

import pytest

from doc_store_server.ingestion import source_normalizer
from doc_store_server.ingestion.source_normalizer import (
    DEFAULT_PRESET,
    FormatFilter,
    normalize_source,
)


DOCUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")


def test_raw_text_normalization_preserves_metadata_and_authoritative_preset() -> None:
    result = normalize_source(
        raw_text="# Source\n\ncontent",
        document_id=DOCUMENT_ID,
        filename="source.md",
        media_type="text/markdown",
        normalization_profile="server-profile-v2",
        chunk_preset="server-authoritative-preset",
    )

    assert result.ok
    assert result.request is not None
    assert result.request.document_id == DOCUMENT_ID
    assert result.request.text == "# Source\n\ncontent"
    assert result.request.selected_filter == "plain_text"
    assert result.request.normalization_profile == "server-profile-v2"
    assert result.request.chunk_preset == "server-authoritative-preset"
    assert result.request.source_metadata.kind == "raw_text"
    assert result.request.source_metadata.filename == "source.md"
    assert result.request.source_metadata.media_type == "text/markdown"
    assert result.request.source_metadata.byte_length == len(result.request.text.encode())
    assert len(result.request.source_metadata.content_sha256) == 64


def test_adapter_shaped_descriptor_and_real_stream_are_accepted() -> None:
    stream = BytesIO(b"transferred text")
    result = normalize_source(
        transferred_file=stream,
        filename="transferred.txt",
        media_type="text/plain",
        document_id=DOCUMENT_ID,
    )

    assert result.ok
    assert result.request is not None
    assert result.request.text == "transferred text"
    assert result.request.source_metadata.kind == "transferred_file"
    assert result.request.source_metadata.filename == "transferred.txt"
    assert result.request.source_metadata.media_type == "text/plain"

    descriptor_result = normalize_source(
        transferred_file={
            "content": b"descriptor text",
            "name": "descriptor.txt",
            "content_type": "text/plain",
        },
        document_id=DOCUMENT_ID,
    )
    assert descriptor_result.ok
    assert descriptor_result.request is not None
    assert descriptor_result.request.text == "descriptor text"
    assert descriptor_result.request.source_metadata.filename == "descriptor.txt"
    assert descriptor_result.request.source_metadata.media_type == "text/plain"


def test_adapter_transfer_reference_is_resolved_from_server_store(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "upload.md"
    path.write_bytes(b"adapter transfer text")

    class Store:
        def get_session(self, transfer_id: str) -> object:
            assert transfer_id == "tr_1"
            return SimpleNamespace(
                direction="upload",
                status="uploaded",
                storage_path=str(path),
                compression="identity",
                filename="upload.md",
            )

    monkeypatch.setattr(source_normalizer, "_adapter_transfer_store", lambda: Store())

    result = normalize_source(
        transferred_file={"transfer_id": "tr_1"},
        document_id=DOCUMENT_ID,
    )

    assert result.ok
    assert result.request is not None
    assert result.request.text == "adapter transfer text"
    assert result.request.source_metadata.filename == "upload.md"


def test_incomplete_adapter_transfer_reference_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Store:
        def get_session(self, _transfer_id: str) -> object:
            return SimpleNamespace(
                direction="upload",
                status="uploading",
                storage_path="/tmp/missing",
                compression="identity",
                filename="upload.md",
            )

    monkeypatch.setattr(source_normalizer, "_adapter_transfer_store", lambda: Store())

    result = normalize_source(
        transferred_file={"transfer_id": "tr_1"},
        document_id=DOCUMENT_ID,
    )

    assert result.diagnostic is not None
    assert result.diagnostic.code == "INCOMPLETE_TRANSFER"


def test_trusted_hint_selects_filter_and_invokes_existing_filter_contract() -> None:
    calls: list[tuple[bytes, str, str | None]] = []

    def apply(payload: bytes, metadata: object) -> str:
        calls.append((payload, metadata.kind, metadata.filename))
        return "filtered output"

    filters = {
        "plain_text": FormatFilter(name="plain_text", apply=lambda payload, _: payload.decode()),
        "custom": FormatFilter(name="custom", media_types=frozenset({"text/custom"}), apply=apply),
    }
    result = normalize_source(
        raw_text="source",
        document_id=DOCUMENT_ID,
        trusted_format_hint="custom",
        media_type="text/plain",
        filters=filters,
    )

    assert result.ok
    assert result.request is not None
    assert result.request.selected_filter == "custom"
    assert result.request.text == "filtered output"
    assert calls == [(b"source", "raw_text", None)]


def test_media_type_and_extension_select_the_same_filter_deterministically() -> None:
    calls: list[str] = []
    filters = {
        "markdown": FormatFilter(
            name="markdown",
            media_types=frozenset({"text/markdown"}),
            extensions=frozenset({".md"}),
            apply=lambda payload, _: calls.append("markdown") or payload.decode(),
        )
    }

    result = normalize_source(
        transferred_file=BytesIO(b"markdown"),
        filename="README.md",
        media_type="TEXT/MARKDOWN",
        filters=filters,
    )

    assert result.ok
    assert result.request is not None
    assert result.request.selected_filter == "markdown"
    assert calls == ["markdown"]


def test_ambiguous_metadata_matches_are_rejected() -> None:
    filters = {
        "one": FormatFilter(name="one", media_types=frozenset({"text/plain"})),
        "two": FormatFilter(name="two", media_types=frozenset({"text/plain"})),
    }

    result = normalize_source(raw_text="ambiguous", filters=filters)

    assert result.diagnostic is not None
    assert result.diagnostic.code == "CONFLICTING_FORMAT_FILTERS"
    assert result.diagnostic.context == {"filters": ["one", "two"]}


def test_identity_is_stable_and_changes_for_content_or_profile() -> None:
    common = {
        "document_id": DOCUMENT_ID,
        "raw_text": "same content",
        "normalization_profile": "profile-a",
        "chunk_preset": "preset-a",
    }
    first = normalize_source(**common)
    second = normalize_source(**common)
    changed_content = normalize_source(**{**common, "raw_text": "changed content"})
    changed_profile = normalize_source(**{**common, "normalization_profile": "profile-b"})

    assert first.ok and second.ok and changed_content.ok and changed_profile.ok
    assert first.request is not None
    assert second.request is not None
    assert changed_content.request is not None
    assert changed_profile.request is not None
    assert first.request.document_id == second.request.document_id
    assert first.request.source_version_id == second.request.source_version_id
    assert changed_content.request.source_version_id != first.request.source_version_id
    assert changed_profile.request.source_version_id != first.request.source_version_id


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({}, "INVALID_SOURCE_COUNT"),
        ({"raw_text": "text", "transferred_file": BytesIO(b"file")}, "INVALID_SOURCE_COUNT"),
        ({"transferred_file": None}, "INVALID_SOURCE_COUNT"),
        ({"raw_text": ""}, "EMPTY_SOURCE"),
        ({"raw_text": "x", "max_source_bytes": 0}, "INVALID_LIMIT"),
        ({"raw_text": "oversized", "max_source_bytes": 4}, "SOURCE_TOO_LARGE"),
        ({"transferred_file": {"content": object()}}, "INVALID_TRANSFER_PAYLOAD"),
        ({"transferred_file": BytesIO(b"x"), "trusted_format_hint": "pdf"}, "UNSUPPORTED_FORMAT_HINT"),
        ({"raw_text": "x", "media_type": "application/octet-stream", "filename": "x.bin"}, "UNSUPPORTED_SOURCE_FORMAT"),
    ],
)
def test_rejections_have_exact_structured_diagnostics(kwargs: dict[str, object], code: str) -> None:
    result = normalize_source(**kwargs)

    assert not result.ok
    assert result.request is None
    assert result.diagnostic is not None
    assert result.diagnostic.code == code
    assert isinstance(result.diagnostic.message, str)
    assert isinstance(result.diagnostic.context, Mapping)


def test_empty_normalized_text_and_filter_failure_are_structured() -> None:
    empty_filter = FormatFilter(name="empty", apply=lambda _payload, _metadata: " \n")
    failed_filter = FormatFilter(name="failed", apply=lambda _payload, _metadata: 1 / 0)

    empty = normalize_source(raw_text="input", trusted_format_hint="empty", filters={"empty": empty_filter})
    failed = normalize_source(raw_text="input", trusted_format_hint="failed", filters={"failed": failed_filter})

    assert empty.diagnostic is not None
    assert empty.diagnostic.code == "EMPTY_NORMALIZED_TEXT"
    assert failed.diagnostic is not None
    assert failed.diagnostic.code == "FILTER_FAILED"
    assert failed.diagnostic.context["filter"] == "failed"
    assert failed.diagnostic.context["error"] == "ZeroDivisionError"


def test_conflicting_filters_are_rejected_before_any_filter_invocation() -> None:
    calls: list[str] = []
    filters = {
        "text": FormatFilter(
            name="text",
            media_types=frozenset({"text/plain"}),
            apply=lambda payload, _: calls.append("text") or payload.decode(),
        ),
        "markdown": FormatFilter(
            name="markdown",
            extensions=frozenset({".md"}),
            apply=lambda payload, _: calls.append("markdown") or payload.decode(),
        ),
    }

    result = normalize_source(
        transferred_file=BytesIO(b"conflict"),
        filename="file.md",
        media_type="text/plain",
        filters=filters,
    )

    assert result.diagnostic is not None
    assert result.diagnostic.code == "CONFLICTING_FORMAT_FILTERS"
    assert result.diagnostic.context == {"filters": ["markdown", "text"]}
    assert calls == []


def test_normalization_has_no_downstream_side_effects_or_local_replacements() -> None:
    downstream_calls: list[str] = []

    def spy_filter(payload: bytes, _metadata: object) -> str:
        downstream_calls.append("filter")
        return payload.decode()

    result = normalize_source(
        raw_text="no downstream work",
        document_id=DOCUMENT_ID,
        filters={
            "plain_text": FormatFilter(
                name="plain_text",
                media_types=frozenset({"text/plain"}),
                apply=spy_filter,
            )
        },
    )

    assert result.ok
    assert downstream_calls == ["filter"]
    assert result.request is not None
    assert not hasattr(result.request, "chunks")
    assert not hasattr(result.request, "embedding")
    assert not hasattr(result.request, "persistence")
    assert not hasattr(result.request, "publication")
    assert not hasattr(result.request, "file_authority")
    assert DEFAULT_PRESET == "technical_text"
