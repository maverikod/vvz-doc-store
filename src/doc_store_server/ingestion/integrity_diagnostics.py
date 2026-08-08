"""Storage-free construction of the controlled integrity diagnostic.

Requirement ``{0o1l}`` does not merely say that a round-trip mismatch makes
ingestion non-successful; it says what the caller is then told: both SHA-256
values, the first differing byte offset or a length boundary when one value is a
strict prefix of the other, policy-bounded context authorized for the caller and
redacted as required, and the relevant Document, Chapter and SemanticChunk
identifiers.

This module owns the one clause of that sentence which is a disclosure decision
rather than a measurement. ``runtime_boundary._integrity_verdict`` has already
measured *where* the two texts part and nothing here recomputes it; what is
decided here is how much of the source may travel alongside that verdict, to
whom, and in what shape.

It is deliberately storage-free. It imports no SQLAlchemy, opens no connection,
and both takes and returns plain values, so the disclosure rules can be
exercised without a database. Turning a ``SourceSpanDiagnostic`` into a
``source_spans`` row belongs to the repository, not here.

Two invariants hold by construction so that no later step can assemble a span
the database would refuse:

* exactly one difference locator is set -- ``byte_offset`` for a mismatch,
  ``length_boundary`` for a strict prefix, never both and never neither, which
  is the ``exactly_one_difference_locator`` check of ``source_spans``;
* a non-empty authorization decision and redaction policy are always present,
  which are its ``authorization_decision_present`` and
  ``redaction_policy_present`` checks.

Disclosure fails closed in both directions, because this diagnostic is produced
on an error path where a mistake would publish source content rather than
withhold it. Only an authorization named in ``CONTEXT_AUTHORIZED_DECISIONS``
receives context at all; any other decision, including one this module has never
heard of, receives none. Only a policy named in ``REDACTION_POLICIES`` is
applied as written; an unrecognised policy name redacts everything rather than
nothing. Redaction masks match-for-character so that redacting can never grow a
window past the limit that bounded it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID, uuid4


INTEGRITY_MISMATCH_CODE: Final[str] = "SOURCE_INTEGRITY_MISMATCH"

#: The verdict vocabulary ``runtime_boundary._integrity_verdict`` produces.
MATCHING_OUTCOME: Final[str] = "match"
MISMATCH_OUTCOME: Final[str] = "mismatch"
LENGTH_BOUNDARY_OUTCOME: Final[str] = "length_boundary"
DIFFERENCE_OUTCOMES: Final[frozenset[str]] = frozenset({MISMATCH_OUTCOME, LENGTH_BOUNDARY_OUTCOME})

#: Authorization decisions that may see bounded source context at all. Every
#: other decision -- an explicit denial, an anonymous caller, or a value this
#: module does not know -- receives no context, only the offsets and hashes.
CONTEXT_AUTHORIZED_DECISIONS: Final[frozenset[str]] = frozenset({"owner", "operator", "auditor"})

_MASK_CHARACTER: Final[str] = "*"

#: Patterns whose matches ``redact_pii`` removes: mail addresses, telephone-like
#: runs, and any long digit run, which is where account, card and document
#: numbers live in prose.
_PII_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\+?\d[\d ()\-]{7,}\d"),
    re.compile(r"\d{4,}"),
)


def _mask(value: str) -> str:
    """Replace every character with the mask, preserving length exactly."""

    return _MASK_CHARACTER * len(value)


def _redact_nothing(value: str) -> str:
    return value


def _redact_pii(value: str) -> str:
    redacted = value
    for pattern in _PII_PATTERNS:
        redacted = pattern.sub(lambda match: _mask(match.group(0)), redacted)
    return redacted


def _redact_everything(value: str) -> str:
    return _mask(value)


#: The recognised redaction policies. An unknown name is not an error but a
#: reason to disclose nothing, so lookups fall back to ``_redact_everything``.
REDACTION_POLICIES: Final[Mapping[str, Callable[[str], str]]] = MappingProxyType(
    {
        "none": _redact_nothing,
        "redact_pii": _redact_pii,
        "redact_all": _redact_everything,
    }
)


@dataclass(frozen=True, slots=True)
class SourceSpanDiagnostic:
    """One bounded, authorized, redacted diagnostic about a refused round trip.

    The fields are exactly the payload the ``source_spans`` table stores, minus
    the producing run, which the diagnostic cannot know because the run is
    written by the evidence transaction that persists this value. ``byte_offset``
    and ``length_boundary`` are mutually exclusive by construction.

    ``character_start`` and ``character_end`` delimit the bounded window the
    diagnostic actually examined -- the same range ``fragment_sha256`` digests --
    not the whole document, which is never read into this value.
    """

    span_id: UUID
    source_state_assertion_id: UUID | None
    document_id: UUID | None
    chapter_id: UUID | None
    chunk_id: UUID | None
    character_start: int | None
    character_end: int | None
    byte_offset: int | None
    length_boundary: int | None
    context_before: str | None
    context_after: str | None
    authorization_decision: str
    redaction_policy: str
    redaction_applied: bool
    page: int | None
    bounding_box: Mapping[str, Any] | None
    fragment_sha256: str | None


def build_integrity_diagnostic(
    *,
    source_text: str,
    reconstructed_text: str,
    integrity_outcome: str,
    first_difference_offset: int | None,
    document_id: UUID | None,
    chapter_id: UUID | None,
    chunk_id: UUID | None,
    authorization: str,
    redaction_policy: str,
    context_limit: int,
    source_state_assertion_id: UUID | None = None,
    page: int | None = None,
    bounding_box: Mapping[str, Any] | None = None,
) -> SourceSpanDiagnostic:
    """Turn a measured non-matching verdict into one disclosable diagnostic.

    ``integrity_outcome`` and ``first_difference_offset`` are the pair
    ``runtime_boundary._integrity_verdict`` returns. A ``match`` is rejected with
    a ``ValueError``: a round trip that agreed has nothing to diagnose, and
    accepting it here would produce a span with no difference locator, which the
    database would refuse anyway.

    The offset is a byte offset into the UTF-8 encoding of the compared texts,
    so it is converted to a character index before any slicing; a locator that
    lands inside a multi-byte character resolves to the boundary before it
    rather than splitting it.

    ``context_limit`` bounds each side independently: ``context_before`` and
    ``context_after`` are at most that many characters taken from
    ``source_text`` around the located point, so the size of the document never
    enters the size of the diagnostic. The redaction policy is applied to both
    before they are stored, and the result is clamped again, so no policy can
    widen a window by substituting for what it removed.
    """

    if integrity_outcome == MATCHING_OUTCOME:
        raise ValueError("a matching round trip has no integrity diagnostic to build")
    if integrity_outcome not in DIFFERENCE_OUTCOMES:
        raise ValueError(f"unknown integrity outcome: {integrity_outcome!r}")
    if source_text == reconstructed_text:
        raise ValueError("identical source and reconstruction cannot carry a difference locator")
    if first_difference_offset is None or first_difference_offset < 0:
        raise ValueError("a difference locator needs a non-negative byte offset")
    if not authorization:
        raise ValueError("a span must name the authorization decision made for its caller")
    if not redaction_policy:
        raise ValueError("a span must name the redaction policy applied to its context")
    if context_limit < 0:
        raise ValueError("a context limit cannot be negative")

    source_bytes = source_text.encode("utf-8")
    reconstructed_bytes = reconstructed_text.encode("utf-8")
    if first_difference_offset > max(len(source_bytes), len(reconstructed_bytes)):
        raise ValueError("the difference locator lies past the end of both compared texts")

    # A strict-prefix boundary can sit at the end of the source, in which case
    # the slice is the whole source and the located index is its length.
    located = len(source_bytes[:first_difference_offset].decode("utf-8", errors="ignore"))
    window_start = max(0, located - context_limit)
    window_end = min(len(source_text), located + context_limit)

    redact = REDACTION_POLICIES.get(redaction_policy, _redact_everything)
    context_before: str | None = None
    context_after: str | None = None
    redaction_applied = False
    if authorization in CONTEXT_AUTHORIZED_DECISIONS:
        exact_before = source_text[window_start:located]
        exact_after = source_text[located:window_end]
        redacted_before = redact(exact_before)
        redacted_after = redact(exact_after)
        redaction_applied = redacted_before != exact_before or redacted_after != exact_after
        context_before = redacted_before[:context_limit]
        context_after = redacted_after[:context_limit]

    # The digest covers the exact window, never the redacted rendering of it, so
    # two runs over the same source correlate even under different policies.
    fragment = source_text[window_start:window_end]
    return SourceSpanDiagnostic(
        span_id=uuid4(),
        source_state_assertion_id=source_state_assertion_id,
        document_id=document_id,
        chapter_id=chapter_id,
        chunk_id=chunk_id,
        character_start=window_start,
        character_end=window_end,
        byte_offset=first_difference_offset if integrity_outcome == MISMATCH_OUTCOME else None,
        length_boundary=(
            first_difference_offset if integrity_outcome == LENGTH_BOUNDARY_OUTCOME else None
        ),
        context_before=context_before,
        context_after=context_after,
        authorization_decision=authorization,
        redaction_policy=redaction_policy,
        redaction_applied=redaction_applied,
        page=page,
        bounding_box=bounding_box,
        fragment_sha256=hashlib.sha256(fragment.encode("utf-8")).hexdigest(),
    )


def _identifier(value: UUID | None) -> str | None:
    return None if value is None else str(value)


class IntegrityRefused(Exception):
    """The single refusal signal a non-matching round trip raises.

    The repository raises it once the evidence is committed and the ingestion
    boundary turns ``controlled_error`` into what the caller receives, so the
    refusal has exactly one shape wherever it is reported.
    """

    def __init__(
        self,
        diagnostic: SourceSpanDiagnostic,
        *,
        source_sha256: str,
        reconstruction_sha256: str,
        evidence_recorded: bool = True,
    ) -> None:
        self.diagnostic = diagnostic
        self.source_sha256 = source_sha256
        self.reconstruction_sha256 = reconstruction_sha256
        self.evidence_recorded = evidence_recorded
        """Whether the failed run and its span were durably committed.

        The refusal itself is a measurement, and it stands whether or not the
        transaction that records it succeeded. Losing the evidence must not also
        lose the diagnosis, so the caller is still told SOURCE_INTEGRITY_MISMATCH
        and told, in the same breath, that this particular refusal left no audit
        row behind. Defaults to True because every path that commits the
        evidence before raising is the ordinary one.
        """

        super().__init__(_refusal_message(diagnostic))

    @property
    def controlled_error(self) -> dict[str, Any]:
        """The caller-facing mapping, JSON-serializable throughout.

        It carries every element ``{0o1l}`` names and nothing else: both hashes,
        the locator that applies, the already-bounded and already-redacted
        context, the authority and policy under which that context was released,
        the span that records all of it, and the Document, Chapter and
        SemanticChunk identifiers. UUIDs are rendered as strings so the mapping
        survives ``json.dumps`` without a custom encoder.
        """

        diagnostic = self.diagnostic
        return {
            "code": INTEGRITY_MISMATCH_CODE,
            "message": str(self),
            "context": {
                "source_sha256": self.source_sha256,
                "reconstruction_sha256": self.reconstruction_sha256,
                "first_difference_offset": diagnostic.byte_offset,
                "length_boundary": diagnostic.length_boundary,
                "context_before": diagnostic.context_before,
                "context_after": diagnostic.context_after,
                "authorization_decision": diagnostic.authorization_decision,
                "redaction_policy": diagnostic.redaction_policy,
                "redaction_applied": diagnostic.redaction_applied,
                "span_id": str(diagnostic.span_id),
                "document_id": _identifier(diagnostic.document_id),
                "chapter_id": _identifier(diagnostic.chapter_id),
                "chunk_id": _identifier(diagnostic.chunk_id),
                # The span_id above names a row that exists only when this is
                # true. A caller told the refusal was measured, and then unable
                # to find the span, would otherwise have to guess whether it was
                # deleted or never written.
                "evidence_recorded": self.evidence_recorded,
            },
        }


def _refusal_message(diagnostic: SourceSpanDiagnostic) -> str:
    if diagnostic.byte_offset is not None:
        located = f"first differ at byte offset {diagnostic.byte_offset}"
    else:
        located = (
            "are a strict prefix of one another up to the length boundary "
            f"{diagnostic.length_boundary}"
        )
    return (
        "Ingestion is not successful: the normalized source and the scope "
        f"reconstructed from it {located}."
    )


__all__ = (
    "CONTEXT_AUTHORIZED_DECISIONS",
    "DIFFERENCE_OUTCOMES",
    "INTEGRITY_MISMATCH_CODE",
    "LENGTH_BOUNDARY_OUTCOME",
    "MATCHING_OUTCOME",
    "MISMATCH_OUTCOME",
    "REDACTION_POLICIES",
    "IntegrityRefused",
    "SourceSpanDiagnostic",
    "build_integrity_diagnostic",
)
