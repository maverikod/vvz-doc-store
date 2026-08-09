"""Unit contracts for runtime reconstruction assembly."""

from __future__ import annotations

import hashlib

from doc_store_server.runtime.text_reconstruction import (
    TextReconstructionService,
    _ChunkTextRow,
)
from doc_store_server.runtime.exact_reconstruction import EXACT_RECONSTRUCTION_KEY


def _row(
    suffix: str,
    *,
    paragraph_id: str,
    order_index: int,
    paragraph_order_index: int,
    text: str,
    exact: dict[str, object] | None = None,
    text_exact: dict[str, object] | None = None,
) -> _ChunkTextRow:
    chunk_block_meta = {EXACT_RECONSTRUCTION_KEY: exact} if exact is not None else {}
    text_block_meta = (
        {EXACT_RECONSTRUCTION_KEY: text_exact}
        if text_exact is not None
        else dict(chunk_block_meta)
    )
    return _ChunkTextRow(
        chunk_id=f"chunk-{suffix}",
        paragraph_id=paragraph_id,
        chapter_id="chapter-1",
        document_id="document-1",
        order_index=order_index,
        paragraph_order_index=paragraph_order_index,
        text=text,
        source_start=order_index * 10,
        source_end=order_index * 10 + len(text),
        source_name="source.md",
        source_path="/tmp/source.md",
        file_id="file-1",
        chunk_block_meta=chunk_block_meta,
        text_block_meta=text_block_meta,
    )


def test_text_reconstruction_assembles_sentences_and_paragraphs_with_ranges() -> None:
    service = TextReconstructionService(None)
    rows = (
        _row("1", paragraph_id="paragraph-1", order_index=1, paragraph_order_index=1, text="First sentence."),
        _row("2", paragraph_id="paragraph-1", order_index=2, paragraph_order_index=1, text="Second sentence."),
        _row("3", paragraph_id="paragraph-2", order_index=1, paragraph_order_index=2, text="Next paragraph."),
    )

    result = service._assemble(
        rows,
        entity="chapter",
        selector={"chapter_id": "chapter-1"},
        include_context=False,
        max_chars=0,
        limit=100,
        offset=0,
    )

    expected_text = "First sentence. Second sentence.\n\nNext paragraph."
    assert result["text"] == expected_text
    assert result["body_sha256"] == hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    assert result["chunk_count"] == 3
    assert result["paragraph_count"] == 2
    assert result["source_names"] == ["source.md"]
    assert result["source_paths"] == ["/tmp/source.md"]
    assert result["range_map"][0]["text_start"] == 0
    assert result["range_map"][1]["text_start"] == len("First sentence. ")
    assert result["range_map"][2]["text_start"] == len("First sentence. Second sentence.\n\n")
    assert result["range_map"][2]["preview"] == "Next paragraph."


def test_text_reconstruction_truncates_without_exceeding_max_chars() -> None:
    service = TextReconstructionService(None)

    result = service._assemble(
        (
            _row("1", paragraph_id="paragraph-1", order_index=1, paragraph_order_index=1, text="abcdef"),
        ),
        entity="source_file",
        selector={"document_id": "document-1"},
        include_context=False,
        max_chars=3,
        limit=100,
        offset=0,
    )

    assert result["text"] == "abc"
    assert result["char_count"] == 3
    assert result["truncated"] is True
    assert result["range_map"][0]["text_end"] == 3


def test_text_reconstruction_omits_range_when_truncation_emits_gap_only() -> None:
    service = TextReconstructionService(None)
    rows = (
        _row(
            "1",
            paragraph_id="paragraph-1",
            order_index=1,
            paragraph_order_index=1,
            text="# Title",
            exact={"version": 1, "gap_before": ""},
        ),
        _row(
            "2",
            paragraph_id="paragraph-2",
            order_index=1,
            paragraph_order_index=2,
            text="- alpha",
            exact={"version": 1, "gap_before": "\n\n", "suffix_after": ""},
        ),
    )

    result = service._assemble(
        rows,
        entity="source_file",
        selector={"document_id": "document-1"},
        include_context=False,
        max_chars=8,
        limit=100,
        offset=0,
    )

    assert result["text"] == "# Title\n"
    assert [item["chunk_id"] for item in result["range_map"]] == ["chunk-1"]
    assert result["range_map"][0]["text_start"] == 0
    assert result["range_map"][0]["text_end"] == len("# Title")
    assert result["truncated"] is True


def test_text_reconstruction_uses_exact_markdown_gaps_and_suffix() -> None:
    service = TextReconstructionService(None)
    rows = (
        _row(
            "1",
            paragraph_id="paragraph-1",
            order_index=1,
            paragraph_order_index=1,
            text="# Title",
            exact={"version": 1, "gap_before": ""},
        ),
        _row(
            "2",
            paragraph_id="paragraph-2",
            order_index=1,
            paragraph_order_index=2,
            text="- alpha",
            exact={"version": 1, "gap_before": "\n\n"},
        ),
        _row(
            "3",
            paragraph_id="paragraph-2",
            order_index=2,
            paragraph_order_index=2,
            text="- beta",
            exact={"version": 1, "gap_before": "\n", "suffix_after": "\n"},
        ),
    )

    result = service._assemble(
        rows,
        entity="source_file",
        selector={"document_id": "document-1"},
        include_context=False,
        max_chars=0,
        limit=100,
        offset=0,
    )

    assert result["text"] == "# Title\n\n- alpha\n- beta\n"
    assert result["range_map"][0]["text_start"] == 0
    assert result["range_map"][1]["text_start"] == len("# Title\n\n")
    assert result["range_map"][2]["text_start"] == len("# Title\n\n- alpha\n")
    assert result["range_map"][2]["text_end"] == len("# Title\n\n- alpha\n- beta")


def test_text_reconstruction_fails_closed_on_mixed_exact_and_legacy_metadata() -> None:
    service = TextReconstructionService(None)
    rows = (
        _row(
            "1",
            paragraph_id="paragraph-1",
            order_index=1,
            paragraph_order_index=1,
            text="Exact",
            exact={"version": 1, "gap_before": "", "suffix_after": ""},
        ),
        _row("2", paragraph_id="paragraph-1", order_index=2, paragraph_order_index=1, text="Legacy"),
    )

    try:
        service._assemble(
            rows,
            entity="source_file",
            selector={"document_id": "document-1"},
            include_context=False,
            max_chars=0,
            limit=100,
            offset=0,
        )
    except ValueError as exc:
        assert "mixed legacy and exact" in str(exc)
    else:
        raise AssertionError("mixed exact and legacy metadata must fail closed")


def test_text_reconstruction_fails_closed_on_malformed_exact_metadata() -> None:
    service = TextReconstructionService(None)

    try:
        service._assemble(
            (
                _row(
                    "1",
                    paragraph_id="paragraph-1",
                    order_index=1,
                    paragraph_order_index=1,
                    text="bad",
                    exact={"version": 1, "gap_before": 1, "suffix_after": ""},
                ),
            ),
            entity="source_file",
            selector={"document_id": "document-1"},
            include_context=False,
            max_chars=0,
            limit=100,
            offset=0,
        )
    except ValueError as exc:
        assert "gap_before" in str(exc)
    else:
        raise AssertionError("malformed exact metadata must fail closed")


def test_text_reconstruction_fails_closed_on_conflicting_exact_metadata() -> None:
    service = TextReconstructionService(None)

    try:
        service._assemble(
            (
                _row(
                    "1",
                    paragraph_id="paragraph-1",
                    order_index=1,
                    paragraph_order_index=1,
                    text="conflict",
                    exact={"version": 1, "gap_before": "", "suffix_after": ""},
                    text_exact={"version": 1, "gap_before": "\n", "suffix_after": ""},
                ),
            ),
            entity="source_file",
            selector={"document_id": "document-1"},
            include_context=False,
            max_chars=0,
            limit=100,
            offset=0,
        )
    except ValueError as exc:
        assert "conflicting exact" in str(exc)
    else:
        raise AssertionError("conflicting exact metadata must fail closed")


def test_text_reconstruction_fails_closed_on_duplicated_final_suffix_metadata() -> None:
    service = TextReconstructionService(None)
    rows = (
        _row(
            "1",
            paragraph_id="paragraph-1",
            order_index=1,
            paragraph_order_index=1,
            text="first",
            exact={"version": 1, "gap_before": "", "suffix_after": ""},
        ),
        _row(
            "2",
            paragraph_id="paragraph-1",
            order_index=2,
            paragraph_order_index=1,
            text="second",
            exact={"version": 1, "gap_before": " ", "suffix_after": ""},
        ),
    )

    try:
        service._assemble(
            rows,
            entity="source_file",
            selector={"document_id": "document-1"},
            include_context=False,
            max_chars=0,
            limit=100,
            offset=0,
        )
    except ValueError as exc:
        assert "one final suffix" in str(exc)
    else:
        raise AssertionError("duplicated suffix metadata must fail closed")
