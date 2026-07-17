"""Unit contracts for runtime reconstruction assembly."""

from __future__ import annotations

import hashlib

from doc_store_server.runtime.text_reconstruction import (
    TextReconstructionService,
    _ChunkTextRow,
)


def _row(
    suffix: str,
    *,
    paragraph_id: str,
    order_index: int,
    paragraph_order_index: int,
    text: str,
) -> _ChunkTextRow:
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
