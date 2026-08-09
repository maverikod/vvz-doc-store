"""Exact byte-for-byte source reconstruction metadata helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final


EXACT_RECONSTRUCTION_KEY: Final[str] = "doc_store_exact_reconstruction"
EXACT_RECONSTRUCTION_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class ExactReconstructionPiece:
    gap_before: str
    source_text: str | None = None
    suffix_after: str | None = None


@dataclass(frozen=True, slots=True)
class ExactReconstructionInput:
    text: str
    source_start: int | None
    source_end: int | None


def build_exact_reconstruction_pieces(
    source_text: str,
    chunks: Sequence[ExactReconstructionInput],
    *,
    scope_start: int = 0,
    scope_end: int | None = None,
    allow_source_text_override: bool = False,
) -> tuple[ExactReconstructionPiece, ...]:
    """Derive exact gaps and final suffix from accepted source slices."""

    if scope_end is None:
        scope_end = len(source_text)
    if scope_start < 0 or scope_end < scope_start or scope_end > len(source_text):
        raise ValueError("exact reconstruction scope is outside the source text")
    cursor = scope_start
    pieces: list[ExactReconstructionPiece] = []
    for chunk in chunks:
        if chunk.source_start is None or chunk.source_end is None:
            raise ValueError("exact reconstruction requires source coordinates")
        start = int(chunk.source_start)
        end = int(chunk.source_end)
        if start < cursor or end < start or end > scope_end:
            raise ValueError("exact reconstruction source ranges must be monotonic and non-overlapping")
        chunk_source_text = source_text[start:end]
        source_text_override = None
        if chunk_source_text != chunk.text:
            if not allow_source_text_override:
                raise ValueError("exact reconstruction source slice does not equal chunk text")
            source_text_override = chunk_source_text
        pieces.append(
            ExactReconstructionPiece(
                gap_before=source_text[cursor:start],
                source_text=source_text_override,
            )
        )
        cursor = end
    if not pieces:
        raise ValueError("exact reconstruction requires at least one chunk")
    pieces[-1] = ExactReconstructionPiece(
        gap_before=pieces[-1].gap_before,
        source_text=pieces[-1].source_text,
        suffix_after=source_text[cursor:scope_end],
    )
    return tuple(pieces)


def metadata_for_piece(piece: ExactReconstructionPiece) -> dict[str, Any]:
    """Serialize one exact reconstruction piece into JSON-compatible metadata."""

    payload: dict[str, Any] = {
        "version": EXACT_RECONSTRUCTION_VERSION,
        "gap_before": piece.gap_before,
    }
    if piece.source_text is not None:
        payload["source_text"] = piece.source_text
    if piece.suffix_after is not None:
        payload["suffix_after"] = piece.suffix_after
    return payload


def merge_piece_metadata(
    metadata: Mapping[str, Any],
    piece: ExactReconstructionPiece,
) -> dict[str, Any]:
    result = dict(metadata)
    result[EXACT_RECONSTRUCTION_KEY] = metadata_for_piece(piece)
    return result


def select_exact_reconstruction_piece(
    chunk_metadata: Mapping[str, Any] | None,
    text_metadata: Mapping[str, Any] | None = None,
) -> ExactReconstructionPiece | None:
    """Return one exact piece, fail-closed on malformed or conflicting metadata."""

    selected: ExactReconstructionPiece | None = None
    for metadata in (chunk_metadata, text_metadata):
        if not isinstance(metadata, Mapping) or EXACT_RECONSTRUCTION_KEY not in metadata:
            continue
        piece = _parse_piece(metadata[EXACT_RECONSTRUCTION_KEY])
        if selected is not None and selected != piece:
            raise ValueError("conflicting exact reconstruction metadata")
        selected = piece
    return selected


def _parse_piece(value: Any) -> ExactReconstructionPiece:
    if not isinstance(value, Mapping):
        raise ValueError("exact reconstruction metadata must be an object")
    if value.get("version") != EXACT_RECONSTRUCTION_VERSION:
        raise ValueError("unsupported exact reconstruction metadata version")
    gap_before = value.get("gap_before")
    if not isinstance(gap_before, str):
        raise ValueError("exact reconstruction gap_before must be a string")
    suffix_after = value.get("suffix_after")
    if suffix_after is not None and not isinstance(suffix_after, str):
        raise ValueError("exact reconstruction suffix_after must be a string")
    source_text = value.get("source_text")
    if source_text is not None and not isinstance(source_text, str):
        raise ValueError("exact reconstruction source_text must be a string")
    return ExactReconstructionPiece(
        gap_before=gap_before,
        source_text=source_text,
        suffix_after=suffix_after,
    )


__all__ = [
    "EXACT_RECONSTRUCTION_KEY",
    "EXACT_RECONSTRUCTION_VERSION",
    "ExactReconstructionInput",
    "ExactReconstructionPiece",
    "build_exact_reconstruction_pieces",
    "merge_piece_metadata",
    "metadata_for_piece",
    "select_exact_reconstruction_piece",
]
