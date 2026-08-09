"""Exact reconstruction metadata contracts."""

from __future__ import annotations

import pytest

from doc_store_server.runtime.exact_reconstruction import (
    ExactReconstructionInput,
    build_exact_reconstruction_pieces,
    metadata_for_piece,
)


def test_exact_reconstruction_pieces_preserve_prefix_gaps_fenced_code_and_suffix() -> None:
    source = "P: # Title\n\n- alpha\n- beta\n\n```py\nprint(1)\n```\n"
    chunks = (
        ExactReconstructionInput(text="# Title", source_start=3, source_end=10),
        ExactReconstructionInput(text="- alpha", source_start=12, source_end=19),
        ExactReconstructionInput(text="- beta", source_start=20, source_end=26),
        ExactReconstructionInput(text="```py\nprint(1)\n```", source_start=28, source_end=46),
    )

    pieces = build_exact_reconstruction_pieces(source, chunks)

    assert [piece.gap_before for piece in pieces] == ["P: ", "\n\n", "\n", "\n\n"]
    assert pieces[-1].suffix_after == "\n"
    assert "".join(
        piece.gap_before + chunk.text
        for piece, chunk in zip(pieces, chunks, strict=True)
    ) + (pieces[-1].suffix_after or "") == source


def test_exact_reconstruction_rejects_overlap_and_text_mismatch() -> None:
    source = "alpha beta"
    with pytest.raises(ValueError, match="monotonic"):
        build_exact_reconstruction_pieces(
            source,
            (
                ExactReconstructionInput(text="alpha", source_start=0, source_end=5),
                ExactReconstructionInput(text="pha", source_start=2, source_end=5),
            ),
        )
    with pytest.raises(ValueError, match="source slice"):
        build_exact_reconstruction_pieces(
            source,
            (ExactReconstructionInput(text="ALPHA", source_start=0, source_end=5),),
        )


def test_exact_reconstruction_rebuild_is_deterministic_complete_and_replaced() -> None:
    source = "alpha\n\nbeta\n"
    chunks = (
        ExactReconstructionInput(text="alpha", source_start=0, source_end=5),
        ExactReconstructionInput(text="beta", source_start=7, source_end=11),
    )

    first = tuple(metadata_for_piece(piece) for piece in build_exact_reconstruction_pieces(source, chunks))
    second = tuple(metadata_for_piece(piece) for piece in build_exact_reconstruction_pieces(source, chunks))
    rebuilt = tuple(
        metadata_for_piece(piece)
        for piece in build_exact_reconstruction_pieces(
            "alpha\n---\nbeta!",
            (
                ExactReconstructionInput(text="alpha", source_start=0, source_end=5),
                ExactReconstructionInput(text="beta", source_start=10, source_end=14),
            ),
        )
    )

    assert first == second
    assert first == (
        {"version": 1, "gap_before": ""},
        {"version": 1, "gap_before": "\n\n", "suffix_after": "\n"},
    )
    assert rebuilt == (
        {"version": 1, "gap_before": ""},
        {"version": 1, "gap_before": "\n---\n", "suffix_after": "!"},
    )
    assert rebuilt != first


def test_exact_reconstruction_can_preserve_source_slice_for_cleaned_markdown_chunk_text() -> None:
    source = "# Title\n\n- alpha\n- beta\n\n```py\nprint(1)\n```\n"
    chunks = (
        ExactReconstructionInput(text="Title", source_start=0, source_end=7),
        ExactReconstructionInput(text="alpha", source_start=9, source_end=16),
        ExactReconstructionInput(text="beta", source_start=17, source_end=23),
        ExactReconstructionInput(text="print(1)", source_start=25, source_end=43),
    )

    pieces = build_exact_reconstruction_pieces(
        source,
        chunks,
        allow_source_text_override=True,
    )

    assert [piece.source_text for piece in pieces] == [
        "# Title",
        "- alpha",
        "- beta",
        "```py\nprint(1)\n```",
    ]
    assert "".join(
        piece.gap_before + (piece.source_text or chunk.text)
        for piece, chunk in zip(pieces, chunks, strict=True)
    ) + (pieces[-1].suffix_after or "") == source
