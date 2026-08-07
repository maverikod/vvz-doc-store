"""Typed document-hierarchy retrieval commands.

The command layer owns parameter and result contracts only.  Retrieval is
provided by the application through the adapter command context.

This module also owns the one shared request-start bitemporal resolution used
by every versioned retrieval, list, context, traversal and ChunkQuery-bound
search operation of a request:

* optional ``act_date`` (effective time) and ``known_at`` (transaction time)
  are accepted anywhere a versioned read is issued;
* omitted values resolve **once** at request start to the same instant, so a
  request that supplies neither observes one coherent pair rather than two
  independently drifting clocks;
* the resolved pair is reused for every operation of the request and is
  returned (or cursor-pinned) whenever the request is bitemporal at all;
* for one logical object at most one ``TemporalAssertion`` version is visible;
  simultaneously accepted assertions for the same object and effective instant
  are rejected as ambiguous instead of being resolved arbitrarily.

Non-temporal callers are unaffected: they pass nothing, receive the same
envelope as before, and never see version internals.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Final, Protocol
from uuid import UUID

from mcp_proxy_adapter.commands.base import Command, CommandResult
from mcp_proxy_adapter.core.errors import ValidationError

from doc_store_server.commands.validation import parse_uuid4

TEMPORAL_PARAMETERS: Final[tuple[str, ...]] = ("act_date", "known_at")
"""Optional bitemporal inputs accepted by every versioned retrieval command."""

TEMPORAL_SCHEMA_PROPERTIES: Final[dict[str, dict[str, Any]]] = {
    "act_date": {
        "type": "string",
        "format": "date-time",
        "description": "Optional effective time; defaults to the request-start instant.",
    },
    "known_at": {
        "type": "string",
        "format": "date-time",
        "description": "Optional transaction time; defaults to the request-start instant.",
    },
}

TEMPORAL_PARAMETER_DOCS: Final[dict[str, dict[str, Any]]] = {
    "act_date": dict(TEMPORAL_SCHEMA_PROPERTIES["act_date"], required=False),
    "known_at": dict(TEMPORAL_SCHEMA_PROPERTIES["known_at"], required=False),
}

CONTEXT_TIME_PAIR_KEY: Final = "resolved_time_pair"
"""Context key carrying the pair already resolved (or cursor-pinned) for the request."""

CONTEXT_REQUEST_NOW_KEY: Final = "request_now"
"""Context key carrying the request-start clock, e.g. PostgreSQL ``NOW()``."""

NON_VISIBLE_ASSERTION_KINDS: Final[frozenset[str]] = frozenset({"deletion"})
"""Assertion kinds whose winning assertion hides the object (see db.schema.ASSERTION_KINDS)."""


class RetrievalBoundary(Protocol):
    """Canonical application retrieval/query boundary used by these commands."""

    async def get_document(self, document_id: UUID, source_version: int | None = None) -> Any:
        """Return one document result."""

    async def get_chapter(self, chapter_id: UUID, source_version: int | None = None) -> Any:
        """Return one chapter result."""

    async def get_paragraph(self, paragraph_id: UUID, source_version: int | None = None) -> Any:
        """Return one paragraph result."""

    async def get_paragraph_by_number(
        self, document_id: UUID, paragraph_number: int, source_version: int | None = None
    ) -> Any:
        """Return one paragraph result by document and 1-based paragraph number."""


class TemporalRetrievalBoundary(RetrievalBoundary, Protocol):
    """Opt-in bitemporal extension of the canonical retrieval boundary.

    A boundary becomes bitemporal by naming ``act_date`` and/or ``known_at``
    explicitly in its signature; the resolved pair is then forwarded to it.
    Boundaries that keep the plain :class:`RetrievalBoundary` signature are
    called exactly as before, which is what keeps current-time defaulting
    transparent for existing callers.
    """

    async def get_document(
        self,
        document_id: UUID,
        source_version: int | None = None,
        act_date: datetime | None = None,
        known_at: datetime | None = None,
    ) -> Any:
        """Return one document result, or the assertions to resolve for it."""

    async def get_chapter(
        self,
        chapter_id: UUID,
        source_version: int | None = None,
        act_date: datetime | None = None,
        known_at: datetime | None = None,
    ) -> Any:
        """Return one chapter result, or the assertions to resolve for it."""

    async def get_paragraph(
        self,
        paragraph_id: UUID,
        source_version: int | None = None,
        act_date: datetime | None = None,
        known_at: datetime | None = None,
    ) -> Any:
        """Return one paragraph result, or the assertions to resolve for it."""

    async def get_paragraph_by_number(
        self,
        document_id: UUID,
        paragraph_number: int,
        source_version: int | None = None,
        act_date: datetime | None = None,
        known_at: datetime | None = None,
    ) -> Any:
        """Return one paragraph result by 1-based number, or its assertions."""


class InvalidVersionError(ValueError):
    """Raised by the retrieval boundary when a requested version is invalid."""


class AmbiguousAssertionError(RuntimeError):
    """Raised when one logical object has several simultaneously accepted assertions.

    Resolution never picks arbitrarily: an ambiguous effective instant is a
    data defect and the request is rejected so the caller can correct it.
    """


@dataclass(frozen=True, slots=True)
class ResolvedTimePair:
    """The request-start effective/transaction pair reused across one request.

    Both members are timezone-aware UTC instants.  The pair is resolved once,
    then reused by every versioned operation of the request and returned or
    cursor-pinned so a later page observes the same two times.
    """

    act_date: datetime
    known_at: datetime

    def as_dict(self) -> dict[str, str]:
        """Return the JSON-safe envelope form of the pair."""

        return {"act_date": self.act_date.isoformat(), "known_at": self.known_at.isoformat()}


def _request_now() -> datetime:
    """Return the fallback request-start instant when the caller pins no clock."""

    return datetime.now(UTC)


def coerce_instant(value: Any, field: str, command_name: str) -> datetime | None:
    """Normalise one optional temporal input to an aware UTC instant.

    ``None`` and the empty/``null`` strings the adapter also strips mean
    "omitted", so the value is defaulted rather than rejected.
    """

    if value is None or isinstance(value, bool):
        if isinstance(value, bool):
            raise ValidationError(
                f"{command_name}: {field} must be an ISO-8601 timestamp",
                data={"field": field, "value": value},
            )
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"null", "none"}:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(
                f"{command_name}: {field} must be an ISO-8601 timestamp",
                data={"field": field, "value": value, "reason": str(exc)},
            ) from exc
    else:
        raise ValidationError(
            f"{command_name}: {field} must be an ISO-8601 timestamp",
            data={"field": field, "value": value},
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def resolve_time_pair(
    act_date: datetime | None = None,
    known_at: datetime | None = None,
    *,
    now: datetime | None = None,
) -> ResolvedTimePair:
    """Resolve the request-start bitemporal pair exactly once.

    Both omitted -> one shared request-start instant for both members; exactly
    one supplied -> that one is preserved and only the other is resolved, from
    the same single ``now`` reading; both supplied -> both preserved.
    """

    request_start = now if now is not None else _request_now()
    if request_start.tzinfo is None:
        request_start = request_start.replace(tzinfo=UTC)
    request_start = request_start.astimezone(UTC)
    return ResolvedTimePair(
        act_date=act_date if act_date is not None else request_start,
        known_at=known_at if known_at is not None else request_start,
    )


def _assertion_field(assertion: Any, name: str) -> Any:
    """Read one assertion field from a mapping row or a typed assertion object."""

    if isinstance(assertion, Mapping):
        return assertion.get(name)
    return getattr(assertion, name, None)


def _as_instant(value: Any) -> datetime | None:
    """Coerce a stored assertion timestamp to an aware UTC instant."""

    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        try:
            return _as_instant(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def is_assertion_like(candidate: Any) -> bool:
    """Report whether a value carries the temporal fields resolution needs."""

    if isinstance(candidate, str | bytes):
        return False
    return (
        _assertion_field(candidate, "effective_from") is not None
        and _assertion_field(candidate, "recorded_at") is not None
    )


def _is_visible(assertion: Any, pair: ResolvedTimePair) -> bool:
    """Report visibility of one assertion at both resolved times.

    Effective time is the half-open interval ``[effective_from,
    effective_until)``; transaction time requires the assertion to already be
    recorded at ``known_at``.
    """

    recorded_at = _as_instant(_assertion_field(assertion, "recorded_at"))
    effective_from = _as_instant(_assertion_field(assertion, "effective_from"))
    if recorded_at is None or effective_from is None or recorded_at > pair.known_at:
        return False
    if effective_from > pair.act_date:
        return False
    effective_until = _as_instant(_assertion_field(assertion, "effective_until"))
    return effective_until is None or pair.act_date < effective_until


def select_visible_assertion(assertions: Iterable[Any], pair: ResolvedTimePair) -> Any | None:
    """Resolve the single assertion visible for one logical object, or ``None``.

    Supersession is derived at known time: an assertion is no longer accepted
    once another assertion recorded at or before ``known_at`` supersedes it, so
    corrections never mutate earlier evidence.  A deletion assertion that wins
    hides the object.  Several survivors mean the same object has ambiguous
    simultaneously accepted assertions at one effective instant, which is
    rejected rather than resolved arbitrarily.
    """

    rows = list(assertions)
    superseded: set[Any] = set()
    for row in rows:
        recorded_at = _as_instant(_assertion_field(row, "recorded_at"))
        target = _assertion_field(row, "supersedes_assertion_id")
        if target is not None and recorded_at is not None and recorded_at <= pair.known_at:
            superseded.add(str(target))

    visible = [
        row
        for row in rows
        if _is_visible(row, pair) and str(_assertion_field(row, "assertion_id")) not in superseded
    ]
    if not visible:
        return None
    if len(visible) > 1:
        object_id = _assertion_field(visible[0], "object_id")
        raise AmbiguousAssertionError(
            f"{len(visible)} simultaneously accepted assertions for object {object_id} "
            f"at act_date={pair.act_date.isoformat()} and known_at={pair.known_at.isoformat()}"
        )
    winner = visible[0]
    if str(_assertion_field(winner, "assertion_kind")) in NON_VISIBLE_ASSERTION_KINDS:
        return None
    return winner


def resolve_visible_versions(
    assertions: Iterable[Any], pair: ResolvedTimePair
) -> dict[str, Any]:
    """Resolve at most one visible version per logical object, keyed by object UUID.

    This runs before authorization, business filtering, list materialization,
    lexical/semantic/hybrid ranking and result limiting, so every later stage
    sees one version per object and never enumerates version internals.
    """

    grouped: dict[str, list[Any]] = {}
    for row in assertions:
        grouped.setdefault(str(_assertion_field(row, "object_id")), []).append(row)
    resolved: dict[str, Any] = {}
    for object_id, rows in grouped.items():
        winner = select_visible_assertion(rows, pair)
        if winner is not None:
            resolved[object_id] = winner
    return resolved


def _typed_identifier(value: Any, field: str, command_name: str) -> UUID:
    """Parse a UUID4 identifier so the boundary receives a typed value."""

    return parse_uuid4(value, field, command_name)


def _typed_result(value: Any) -> Any:
    """Convert common typed result models without changing their public fields."""

    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return value


class _RetrievalCommand(Command):
    """Shared adapter contract and execution/error mapping for retrieval commands."""

    category: ClassVar[str] = "retrieval"
    author: ClassVar[str] = "Vasiliy Zdanovskiy"
    email: ClassVar[str] = "vasilyvz@gmail.com"
    version: ClassVar[str] = "0.1.0"
    use_queue: ClassVar[bool] = False
    identifier_field: ClassVar[str]
    entity_name: ClassVar[str]
    boundary_method: ClassVar[str]
    descr: ClassVar[str]
    detailed_description: ClassVar[str]
    schema_properties: ClassVar[dict[str, dict[str, Any]]]
    required_fields: ClassVar[tuple[str, ...]]
    parameter_docs: ClassVar[dict[str, dict[str, Any]]]
    return_contract: ClassVar[dict[str, Any]]
    usage_examples: ClassVar[list[dict[str, Any]]]
    best_practices: ClassVar[list[str]]
    retrieval_boundary: ClassVar[RetrievalBoundary | None] = None

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {key: dict(value) for key, value in cls.schema_properties.items()},
            "required": list(cls.required_fields),
            "additionalProperties": False,
        }

    @classmethod
    def temporal_schema_properties(cls) -> dict[str, dict[str, Any]]:
        """Return the JSON-schema fragment of the optional bitemporal inputs.

        Accepted by :meth:`validate_params` and documented in :meth:`metadata`.
        """

        return {key: dict(value) for key, value in TEMPORAL_SCHEMA_PROPERTIES.items()}

    @classmethod
    def temporal_parameter_docs(cls) -> dict[str, dict[str, Any]]:
        """Return the documented form of the optional bitemporal inputs.

        Same two parameters as :meth:`temporal_schema_properties`, carrying the
        ``required: False`` marker the metadata contract uses.
        """

        return {key: dict(value) for key, value in TEMPORAL_PARAMETER_DOCS.items()}

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "version": cls.version,
            "description": cls.descr,
            "category": cls.category,
            "author": cls.author,
            "email": cls.email,
            "detailed_description": cls.detailed_description,
            "parameters": cls.parameter_docs,
            "return_value": cls.return_contract,
            "usage_examples": cls.usage_examples,
            "error_cases": {
                "NOT_FOUND": {
                    "description": f"The requested {cls.entity_name} is not visible.",
                    "message": f"{cls.entity_name.title()} not found.",
                    "solution": "Retry with an identifier returned by the canonical query boundary.",
                },
                "INVALID_VERSION": {
                    "description": "The requested source version is not valid for the entity.",
                    "message": "The requested source_version is invalid.",
                    "solution": "Omit source_version for the current version or use a visible positive version.",
                },
                "AMBIGUOUS_ASSERTION": {
                    "description": (
                        "Several assertions are simultaneously accepted for the same object "
                        "and effective instant."
                    ),
                    "message": "Bitemporal resolution is ambiguous.",
                    "solution": "Correct the conflicting assertions or narrow act_date/known_at.",
                },
                "INTERNAL_ERROR": {
                    "description": "The canonical retrieval boundary failed unexpectedly.",
                    "message": "Retrieval failed internally.",
                    "solution": "Inspect the service diagnostics and retry the command.",
                },
            },
            "best_practices": cls.best_practices,
        }

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = dict(params) if params else {}
        temporal_inputs = {name: raw.pop(name) for name in TEMPORAL_PARAMETERS if name in raw}
        validated = super().validate_params(raw)
        validated[self.identifier_field] = _typed_identifier(
            validated[self.identifier_field], self.identifier_field, self.name
        )
        source_version = validated.get("source_version")
        if source_version is not None and (
            isinstance(source_version, bool)
            or not isinstance(source_version, int)
            or source_version <= 0
        ):
            raise ValidationError(
                f"{self.name}: source_version must be a positive integer",
                data={"field": "source_version", "value": source_version},
            )
        for name, value in temporal_inputs.items():
            instant = coerce_instant(value, name, self.name)
            if instant is not None:
                validated[name] = instant
        return validated

    def _boundary(self, context: Any) -> Any | None:
        """Return the canonical retrieval boundary for this request, if any."""

        boundary = context.get("retrieval_boundary") if isinstance(context, Mapping) else None
        if boundary is None:
            boundary = self.retrieval_boundary
        if boundary is None or not hasattr(boundary, self.boundary_method):
            return None
        return boundary

    def _request_time_pair(
        self, kwargs: dict[str, Any], context: Any
    ) -> tuple[ResolvedTimePair, bool]:
        """Resolve the request-start pair once and report whether it is pinned.

        A pair already resolved earlier in the request (list page, traversal,
        ChunkQuery search, or a stable cursor) arrives through the context and
        is reused verbatim for whatever the caller left omitted, so one request
        never reads the clock twice.
        """

        supplied = {
            name: coerce_instant(kwargs.pop(name, None), name, self.name)
            for name in TEMPORAL_PARAMETERS
        }
        pinned = any(value is not None for value in supplied.values())

        inherited = context.get(CONTEXT_TIME_PAIR_KEY) if isinstance(context, Mapping) else None
        if isinstance(inherited, Mapping):
            inherited = resolve_time_pair(
                coerce_instant(inherited.get("act_date"), "act_date", self.name),
                coerce_instant(inherited.get("known_at"), "known_at", self.name),
            )
        if isinstance(inherited, ResolvedTimePair):
            return (
                ResolvedTimePair(
                    act_date=supplied["act_date"] or inherited.act_date,
                    known_at=supplied["known_at"] or inherited.known_at,
                ),
                True,
            )

        now = context.get(CONTEXT_REQUEST_NOW_KEY) if isinstance(context, Mapping) else None
        if callable(now):
            now = now()
        pair = resolve_time_pair(
            supplied["act_date"],
            supplied["known_at"],
            now=coerce_instant(now, CONTEXT_REQUEST_NOW_KEY, self.name),
        )
        return pair, pinned

    def _temporal_call_kwargs(self, method: Any, pair: ResolvedTimePair) -> dict[str, datetime]:
        """Forward the resolved pair only to a boundary that declares it.

        Signature gating is what keeps the defaults transparent: a boundary
        written against the plain :class:`RetrievalBoundary` contract is still
        called with exactly its historical arguments.
        """

        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            return {}
        accepted = {
            name
            for name, parameter in parameters.items()
            if name in TEMPORAL_PARAMETERS
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        return {name: getattr(pair, name) for name in TEMPORAL_PARAMETERS if name in accepted}

    def _visible_value(self, result: Any, pair: ResolvedTimePair) -> Any:
        """Reduce assertion evidence to the one version visible at both times.

        Ordinary results pass through untouched, so get results stay
        version-transparent and never enumerate all versions of an object.
        """

        if is_assertion_like(result):
            return select_visible_assertion([result], pair)
        if isinstance(result, Sequence) and not isinstance(result, str | bytes):
            rows = list(result)
            if rows and all(is_assertion_like(row) for row in rows):
                return select_visible_assertion(rows, pair)
        return result

    async def _call_boundary(
        self, boundary: Any, forwarded: Mapping[str, datetime], *args: Any
    ) -> Any:
        """Invoke the boundary method, forwarding the pair only when it accepts it."""

        result = getattr(boundary, self.boundary_method)(*args, **forwarded)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def execute(self, **kwargs: Any) -> CommandResult:
        context = kwargs.pop("context", {})
        boundary = self._boundary(context)
        if boundary is None:
            return CommandResult(success=False, error="INTERNAL_ERROR: retrieval boundary unavailable")

        pair, pinned = self._request_time_pair(kwargs, context)
        identifier = kwargs[self.identifier_field]
        source_version = kwargs.get("source_version")
        forwarded = self._temporal_call_kwargs(getattr(boundary, self.boundary_method), pair)
        try:
            result = await self._call_boundary(boundary, forwarded, identifier, source_version)
            visible = self._visible_value(result, pair)
        except AmbiguousAssertionError as exc:
            return CommandResult(success=False, error=f"AMBIGUOUS_ASSERTION: {exc}")
        except InvalidVersionError as exc:
            return CommandResult(success=False, error=f"INVALID_VERSION: {exc}")
        except LookupError as exc:
            return CommandResult(success=False, error=f"NOT_FOUND: {exc}")
        except Exception as exc:
            return CommandResult(success=False, error=f"INTERNAL_ERROR: {exc}")

        if visible is None and result is not None:
            return CommandResult(
                success=False,
                error=f"NOT_FOUND: no visible {self.entity_name} version at the resolved times",
            )

        data: dict[str, Any] = {
            "entity": self.entity_name,
            "identifier": str(identifier),
            "source_version": source_version,
            "value": _typed_result(visible),
        }
        if pinned or forwarded:
            data.update(pair.as_dict())
        return CommandResult(success=True, data=data)


class DocumentGetCommand(_RetrievalCommand):
    name = "document_get"
    identifier_field = "document_id"
    entity_name = "document"
    boundary_method = "get_document"
    descr = "Retrieve one typed document by UUID."
    detailed_description = (
        "Validates a document UUID, optional positive source version and optional act_date/known_at "
        "bounds, resolves the request-start bitemporal pair once, then delegates to the canonical "
        "retrieval boundary and returns the single version visible at both times."
    )
    schema_properties = {
        "document_id": {"type": "string", "description": "Document UUID4 identifier."},
        "source_version": {"type": "integer", "description": "Optional positive document source version."},
        **_RetrievalCommand.temporal_schema_properties(),
    }
    required_fields = ("document_id",)
    parameter_docs = {
        "document_id": {"type": "string", "description": "Document UUID4 identifier.", "required": True},
        "source_version": {"type": "integer", "description": "Optional positive document source version.", "required": False},
        **_RetrievalCommand.temporal_parameter_docs(),
    }
    return_contract = {
        "description": "Stable typed document retrieval envelope.",
        "data": {
            "value": "Document data.",
            "act_date": "Resolved effective time, present for bitemporal requests.",
            "known_at": "Resolved transaction time, present for bitemporal requests.",
        },
    }
    usage_examples = [{"document_id": "550e8400-e29b-41d4-a716-446655440000"}]
    best_practices = [
        "Use identifiers returned by the canonical document query boundary.",
        "Omit act_date and known_at for current state; both then resolve to one request-start instant.",
    ]

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return super().get_schema()

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return super().metadata()

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return super().validate_params(params)

    async def execute(self, **kwargs: Any) -> CommandResult:
        return await super().execute(**kwargs)


class ChapterGetCommand(_RetrievalCommand):
    name = "chapter_get"
    identifier_field = "chapter_id"
    entity_name = "chapter"
    boundary_method = "get_chapter"
    descr = "Retrieve one typed chapter by UUID."
    detailed_description = "Validates a chapter UUID and optional positive source version, then delegates to the canonical retrieval boundary."
    schema_properties = {
        "chapter_id": {"type": "string", "description": "Chapter UUID4 identifier."},
        "source_version": dict(DocumentGetCommand.schema_properties["source_version"]),
        **_RetrievalCommand.temporal_schema_properties(),
    }
    required_fields = ("chapter_id",)
    parameter_docs = {
        "chapter_id": {"type": "string", "description": "Chapter UUID4 identifier.", "required": True},
        "source_version": dict(DocumentGetCommand.parameter_docs["source_version"]),
        **_RetrievalCommand.temporal_parameter_docs(),
    }
    return_contract = {"description": "Stable typed chapter retrieval envelope.", "data": {"value": "Chapter data."}}
    usage_examples = [{"chapter_id": "550e8400-e29b-41d4-a716-446655440000"}]
    best_practices = ["Use chapter identifiers returned by the canonical document hierarchy."]

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return super().get_schema()

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return super().metadata()

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return super().validate_params(params)

    async def execute(self, **kwargs: Any) -> CommandResult:
        return await super().execute(**kwargs)


class ParagraphGetCommand(_RetrievalCommand):
    name = "paragraph_get"
    identifier_field = "paragraph_id"
    entity_name = "paragraph"
    boundary_method = "get_paragraph"
    descr = "Retrieve one typed paragraph by UUID."
    detailed_description = "Validates a paragraph UUID and optional positive source version, then delegates to the canonical retrieval boundary."
    schema_properties = {
        "paragraph_id": {"type": "string", "description": "Paragraph UUID4 identifier."},
        "source_version": dict(DocumentGetCommand.schema_properties["source_version"]),
        **_RetrievalCommand.temporal_schema_properties(),
    }
    required_fields = ("paragraph_id",)
    parameter_docs = {
        "paragraph_id": {"type": "string", "description": "Paragraph UUID4 identifier.", "required": True},
        "source_version": dict(DocumentGetCommand.parameter_docs["source_version"]),
        **_RetrievalCommand.temporal_parameter_docs(),
    }
    return_contract = {"description": "Stable typed paragraph retrieval envelope.", "data": {"value": "Paragraph data."}}
    usage_examples = [{"paragraph_id": "550e8400-e29b-41d4-a716-446655440000"}]
    best_practices = ["Use paragraph identifiers returned by the canonical chapter hierarchy."]

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        return super().get_schema()

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return super().metadata()

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return super().validate_params(params)

    async def execute(self, **kwargs: Any) -> CommandResult:
        return await super().execute(**kwargs)


class ParagraphGetByNumberCommand(_RetrievalCommand):
    name = "paragraph_get_by_number"
    identifier_field = "document_id"
    entity_name = "paragraph"
    boundary_method = "get_paragraph_by_number"
    descr = "Retrieve paragraph text by document UUID and 1-based paragraph number."
    detailed_description = (
        "Validates a document UUID, 1-based paragraph_number and optional positive "
        "source version, then resolves the paragraph by canonical source order."
    )
    schema_properties = {
        "document_id": {"type": "string", "description": "Document UUID4 identifier."},
        "paragraph_number": {
            "type": "integer",
            "minimum": 1,
            "description": "1-based paragraph number in canonical document source order.",
        },
        "source_version": {"type": "integer", "description": "Optional positive document source version."},
        **_RetrievalCommand.temporal_schema_properties(),
    }
    required_fields = ("document_id", "paragraph_number")
    parameter_docs = {
        "document_id": {"type": "string", "description": "Document UUID4 identifier.", "required": True},
        "paragraph_number": {
            "type": "integer",
            "description": "1-based paragraph number in canonical document source order.",
            "required": True,
        },
        "source_version": dict(DocumentGetCommand.parameter_docs["source_version"]),
        **_RetrievalCommand.temporal_parameter_docs(),
    }
    return_contract = {
        "description": "Stable paragraph lookup envelope.",
        "data": {
            "document_id": "Document UUID.",
            "paragraph_number": "1-based paragraph number.",
            "text": "Paragraph text.",
            "value": "Full paragraph data.",
            "act_date": "Resolved effective time, present for bitemporal requests.",
            "known_at": "Resolved transaction time, present for bitemporal requests.",
        },
    }
    usage_examples = [
        {
            "document_id": "550e8400-e29b-41d4-a716-446655440000",
            "paragraph_number": 2,
        }
    ]
    best_practices = [
        "Use paragraph_number as a 1-based human-facing index in document source order.",
        "Use paragraph_get when you already have the paragraph UUID.",
    ]

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        validated = super().validate_params(params)
        paragraph_number = validated.get("paragraph_number")
        if (
            isinstance(paragraph_number, bool)
            or not isinstance(paragraph_number, int)
            or paragraph_number < 1
        ):
            raise ValidationError(
                f"{self.name}: paragraph_number must be a positive integer",
                data={"field": "paragraph_number", "value": paragraph_number},
            )
        return validated

    async def execute(self, **kwargs: Any) -> CommandResult:
        context = kwargs.pop("context", {})
        boundary = self._boundary(context)
        if boundary is None:
            return CommandResult(success=False, error="INTERNAL_ERROR: retrieval boundary unavailable")

        pair, pinned = self._request_time_pair(kwargs, context)
        document_id = kwargs["document_id"]
        paragraph_number = kwargs["paragraph_number"]
        source_version = kwargs.get("source_version")
        forwarded = self._temporal_call_kwargs(getattr(boundary, self.boundary_method), pair)
        try:
            result = await self._call_boundary(
                boundary, forwarded, document_id, paragraph_number, source_version
            )
            visible = self._visible_value(result, pair)
        except AmbiguousAssertionError as exc:
            return CommandResult(success=False, error=f"AMBIGUOUS_ASSERTION: {exc}")
        except InvalidVersionError as exc:
            return CommandResult(success=False, error=f"INVALID_VERSION: {exc}")
        except LookupError as exc:
            return CommandResult(success=False, error=f"NOT_FOUND: {exc}")
        except Exception as exc:
            return CommandResult(success=False, error=f"INTERNAL_ERROR: {exc}")

        if visible is None and result is not None:
            return CommandResult(
                success=False,
                error=f"NOT_FOUND: no visible {self.entity_name} version at the resolved times",
            )

        value = _typed_result(visible)
        text_value = value.get("text") if isinstance(value, Mapping) else None
        data: dict[str, Any] = {
            "entity": self.entity_name,
            "identifier": str(value.get("id")) if isinstance(value, Mapping) and value.get("id") else None,
            "document_id": str(document_id),
            "paragraph_number": paragraph_number,
            "source_version": source_version,
            "text": text_value,
            "value": value,
        }
        if pinned or forwarded:
            data.update(pair.as_dict())
        return CommandResult(success=True, data=data)


__all__ = [
    "AmbiguousAssertionError",
    "ChapterGetCommand",
    "DocumentGetCommand",
    "InvalidVersionError",
    "ParagraphGetByNumberCommand",
    "ParagraphGetCommand",
    "ResolvedTimePair",
    "RetrievalBoundary",
    "TEMPORAL_PARAMETERS",
    "TEMPORAL_PARAMETER_DOCS",
    "TEMPORAL_SCHEMA_PROPERTIES",
    "TemporalRetrievalBoundary",
    "coerce_instant",
    "is_assertion_like",
    "resolve_time_pair",
    "resolve_visible_versions",
    "select_visible_assertion",
]
