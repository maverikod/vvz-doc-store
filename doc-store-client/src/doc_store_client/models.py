"""Stable, transport-neutral public contracts for ``doc-store-client``.

The models intentionally contain validation and payload conversion only.  They
do not know how a command is sent, queued, retried, persisted, or transferred.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime
from hmac import compare_digest
from typing import Any, ClassVar, Final, Iterable, Mapping, Self
from uuid import UUID

from chunk_metadata_adapter import ChunkQuery


Payload = Mapping[str, Any]


def _payload(model: Any, *, omit_none: bool = True) -> dict[str, Any]:
    """Return fields in declaration order using their public server names."""

    result: dict[str, Any] = {}
    for model_field in fields(model):
        value = getattr(model, model_field.name)
        if omit_none and value is None:
            continue
        result[model_field.name] = _json_ready(value)
    return result


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _read(cls: type[Any], payload: Payload) -> Any:
    """Build a model while rejecting unknown server fields deterministically."""

    known = {model_field.name for model_field in fields(cls)}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ValueError(f"unknown {cls.__name__} fields: {', '.join(unknown)}")
    return cls(
        **{
            model_field.name: payload[model_field.name]
            for model_field in fields(cls)
            if model_field.name in payload
        }
    )


class PublicModel:
    """Small common API shared by all public payload models."""

    _omit_none: ClassVar[bool] = True

    def to_params(self) -> dict[str, Any]:
        return _payload(self, omit_none=self._omit_none)

    def to_payload(self) -> dict[str, Any]:
        return self.to_params()

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        if not isinstance(payload, Mapping):
            raise TypeError(f"{cls.__name__} payload must be an object")
        return _read(cls, payload)


#
# Bitemporal request bounds, resolved-pair disclosure and the stable cursor.
#
# A request never resolves ``act_date``/``known_at`` itself.  Omitted bounds stay
# omitted on the wire so the one shared request-start resolver remains their sole
# source; the pair it resolved comes back disclosed on the result or pinned inside
# a cursor.  A cursor may only be replayed against the very pair, ordering and
# canonical query it pins, which is what keeps a paged read stable.
#

TEMPORAL_BOUND_FIELDS: Final[tuple[str, str]] = ("act_date", "known_at")
"""Optional bitemporal request bounds understood by versioned reads."""

_QUERY_IDENTITY_EXCLUDED: Final[frozenset[str]] = frozenset(
    {"act_date", "known_at", "cursor", "offset"}
)
"""Pinned separately by the cursor, so paging never changes the query identity."""

_OBJECT_IDENTITY_KEYS: Final[tuple[str, ...]] = (
    "logical_chunk_id",
    "entity_id",
    "object_id",
    "chunk_id",
    "id",
    "uuid",
)
"""Preferred order of the keys naming the logical object behind a listed item."""


def _parse_instant(value: Any, field_name: str) -> datetime:
    """Parse one ISO-8601 instant, rejecting anything else deterministically."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from None


def _validate_instant(value: Any, field_name: str) -> str:
    _parse_instant(value, field_name)
    return value.strip()


def _validate_optional_instants(**values: Any) -> None:
    for field_name, value in values.items():
        if value is not None:
            _validate_instant(value, field_name)


def _same_instant(left: str, right: str, field_name: str) -> bool:
    """Compare two instants by the moment they denote, not by their spelling."""

    return _parse_instant(left, field_name) == _parse_instant(right, field_name)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _query_identity_payload(query: Any) -> dict[str, Any]:
    if isinstance(query, PublicModel):
        payload: Mapping[str, Any] = query.to_params()
    elif hasattr(query, "model_dump"):
        payload = query.model_dump(mode="python", exclude_none=True, exclude_unset=True)
    elif isinstance(query, Mapping):
        payload = {key: value for key, value in query.items() if value is not None}
    else:
        raise TypeError("query must be a public model, a ChunkQuery, or a mapping")
    return {
        key: _json_ready(value)
        for key, value in payload.items()
        if key not in _QUERY_IDENTITY_EXCLUDED
    }


def canonical_query_id(query: Any) -> str:
    """Return the canonical identity of one list or ChunkQuery search request.

    Temporal bounds and continuation state are excluded on purpose: the cursor
    pins those separately, so turning a page never changes the query identity
    while any change to the query itself does.
    """

    canonical = _canonical_json(_query_identity_payload(query))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def with_temporal_bounds(
    query: ChunkQuery,
    *,
    act_date: str | None = None,
    known_at: str | None = None,
) -> ChunkQuery:
    """Return ``query`` carrying optional bitemporal bounds.

    ``ChunkQuery`` stays the sole public full-text, semantic and hybrid search
    contract: the bounds ride on it instead of on a parallel search model, and
    omitted bounds are left unset for the request-start resolver.
    """

    _validate_optional_instants(act_date=act_date, known_at=known_at)
    bounds = {
        name: value
        for name, value in (("act_date", act_date), ("known_at", known_at))
        if value is not None
    }
    if not bounds:
        return query
    return query.model_copy(update=bounds)


def temporal_bounds(query: ChunkQuery) -> tuple[str | None, str | None]:
    """Return the optional ``(act_date, known_at)`` bounds carried by a query."""

    return (getattr(query, "act_date", None), getattr(query, "known_at", None))


def _reject_repeated_objects(items: Iterable[Any], *, context: str) -> None:
    """Reject a version-transparent listing that exposes one object twice.

    After temporal resolution exactly one visible version exists per logical
    object, so a repeated identity means the listing enumerated versions.
    """

    seen: set[tuple[str, str]] = set()
    for item in items:
        payload = item if isinstance(item, Mapping) else getattr(item, "__dict__", None)
        if not isinstance(payload, Mapping):
            continue
        for key in _OBJECT_IDENTITY_KEYS:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                continue
            marker = (key, value)
            if marker in seen:
                raise ValueError(
                    f"{context} must expose one visible version per object; "
                    f"{key}={value} appears more than once"
                )
            seen.add(marker)
            break


@dataclass(frozen=True, kw_only=True)
class TemporalCursor(PublicModel):
    """Stable pagination cursor pinning one resolved bitemporal page.

    The cursor binds the resolved ``act_date``/``known_at`` pair, the
    deterministic ordering key, the continuation position and the canonical
    query identity, and carries an integrity hash over exactly those values.
    """

    act_date: str
    known_at: str
    order_key: str
    query_id: str
    position: int = 0
    integrity: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "act_date", _validate_instant(self.act_date, "act_date"))
        object.__setattr__(self, "known_at", _validate_instant(self.known_at, "known_at"))
        if not isinstance(self.order_key, str) or not self.order_key.strip():
            raise ValueError("order_key must be non-empty")
        if not isinstance(self.query_id, str) or not self.query_id.strip():
            raise ValueError("query_id must be non-empty")
        if (
            isinstance(self.position, bool)
            or not isinstance(self.position, int)
            or self.position < 0
        ):
            raise ValueError("position must be a non-negative integer")
        expected = self._expected_integrity()
        if not self.integrity:
            object.__setattr__(self, "integrity", expected)
        elif not compare_digest(str(self.integrity), expected):
            raise ValueError("cursor integrity hash does not match its pinned state")

    def _expected_integrity(self) -> str:
        pinned = {
            "act_date": self.act_date,
            "known_at": self.known_at,
            "order_key": self.order_key,
            "position": self.position,
            "query_id": self.query_id,
        }
        return hashlib.sha256(_canonical_json(pinned).encode("utf-8")).hexdigest()

    @property
    def resolved_pair(self) -> tuple[str, str]:
        """Return the resolved ``(act_date, known_at)`` pair pinned here."""

        return (self.act_date, self.known_at)

    @classmethod
    def pin(
        cls,
        *,
        act_date: str,
        known_at: str,
        order_key: str,
        query: Any,
        position: int = 0,
    ) -> Self:
        """Pin one page of ``query`` to its resolved pair and ordering."""

        return cls(
            act_date=act_date,
            known_at=known_at,
            order_key=order_key,
            query_id=canonical_query_id(query),
            position=position,
        )

    def advance(self, position: int) -> Self:
        """Return the next page cursor, re-sealed over the new position."""

        return type(self)(
            act_date=self.act_date,
            known_at=self.known_at,
            order_key=self.order_key,
            query_id=self.query_id,
            position=position,
        )

    def encode(self) -> str:
        """Return the opaque, URL-safe token form of this cursor."""

        raw = _canonical_json(self.to_params()).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Self:
        """Rebuild a cursor from its token, rejecting a tampered payload."""

        if not isinstance(token, str) or not token.strip():
            raise ValueError("cursor must be a non-empty token")
        text = token.strip()
        try:
            raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError):
            raise ValueError("cursor is not a valid cursor token") from None
        if not isinstance(payload, Mapping):
            raise ValueError("cursor is not a valid cursor token")
        return cls.from_payload(payload)

    def validate_replay(
        self,
        *,
        act_date: str | None = None,
        known_at: str | None = None,
        order_key: str | None = None,
        query_id: str | None = None,
        query: Any = None,
    ) -> None:
        """Reject replay against a different pair, ordering, or query identity.

        Only the values actually supplied are checked; anything omitted is
        simply not being replayed and therefore cannot disagree.
        """

        if query is not None:
            if query_id is not None:
                raise ValueError("pass either query or query_id, not both")
            query_id = canonical_query_id(query)
        mismatched = [
            name
            for name, supplied, pinned in (
                ("act_date", act_date, self.act_date),
                ("known_at", known_at, self.known_at),
            )
            if supplied is not None and not _same_instant(supplied, pinned, name)
        ]
        mismatched += [
            name
            for name, supplied, pinned in (
                ("order_key", order_key, self.order_key),
                ("query_id", query_id, self.query_id),
            )
            if supplied is not None and supplied != pinned
        ]
        if mismatched:
            raise ValueError(
                "cursor replay rejected: "
                f"{', '.join(mismatched)} differ from the values pinned in the cursor"
            )


@dataclass(frozen=True, kw_only=True)
class TemporalPayload(PublicModel):
    """Optional bitemporal bounds plus the cursor that may pin them."""

    act_date: str | None = None
    known_at: str | None = None
    cursor: str | None = None

    def pinned_cursor(self) -> TemporalCursor | None:
        """Return the decoded cursor, or ``None`` when none is carried."""

        return None if self.cursor is None else TemporalCursor.decode(self.cursor)


@dataclass(frozen=True, kw_only=True)
class TemporalRequest(TemporalPayload):
    """Optional bitemporal bounds accepted by versioned reads.

    Both bounds are optional and are never defaulted here: omitting them keeps
    them off the wire so the shared request-start resolver stays their sole
    source.  A cursor already pins a resolved pair and a canonical query, so
    bounds repeated next to one must agree with it.
    """

    def __post_init__(self) -> None:
        _validate_optional_instants(act_date=self.act_date, known_at=self.known_at)
        pinned = self.pinned_cursor()
        if pinned is not None:
            pinned.validate_replay(
                act_date=self.act_date,
                known_at=self.known_at,
                query_id=self.query_id(),
            )

    def query_id(self) -> str:
        """Return the canonical identity of this request as a query."""

        return canonical_query_id(self)

    def resolved_pair(self) -> tuple[str, str] | None:
        """Return the pair pinned by the cursor, if this request carries one."""

        pinned = self.pinned_cursor()
        return None if pinned is None else pinned.resolved_pair


@dataclass(frozen=True, kw_only=True)
class TemporalResult(TemporalPayload):
    """Result-side disclosure of the resolved bitemporal pair.

    A bitemporal result either discloses ``act_date`` and ``known_at`` directly
    or carries a cursor pinning them; when it does both, the two must agree.
    """

    def __post_init__(self) -> None:
        _validate_optional_instants(act_date=self.act_date, known_at=self.known_at)
        if (self.act_date is None) != (self.known_at is None):
            raise ValueError("act_date and known_at must be disclosed together")
        pinned = self.pinned_cursor()
        if pinned is not None and self.act_date is not None:
            pinned.validate_replay(act_date=self.act_date, known_at=self.known_at)

    def resolved_pair(self) -> tuple[str, str] | None:
        """Return the resolved pair, disclosed directly or through the cursor."""

        if self.act_date is not None and self.known_at is not None:
            return (self.act_date, self.known_at)
        pinned = self.pinned_cursor()
        return None if pinned is None else pinned.resolved_pair


@dataclass(frozen=True, kw_only=True)
class DocumentWriteRequest(PublicModel):
    """Shared source contract for document create and update."""

    document_id: str
    source_version_id: str
    raw_text: str | None = None
    transferred_file: Mapping[str, Any] | None = None
    chunking_strategy: str | None = None

    def __post_init__(self) -> None:
        if not self.document_id.strip() or not self.source_version_id.strip():
            raise ValueError("document_id and source_version_id must be non-empty")
        if self.raw_text is not None and self.transferred_file is not None:
            raise ValueError("raw_text and transferred_file are mutually exclusive")
        if self.raw_text is not None and not self.raw_text:
            raise ValueError("raw_text must be non-empty")
        if self.chunking_strategy is not None and self.chunking_strategy not in {
            "paragraph",
            "sentence",
            "semantic",
        }:
            raise ValueError("chunking_strategy must be paragraph, sentence, or semantic")


@dataclass(frozen=True, kw_only=True)
class DocumentCreateRequest(DocumentWriteRequest):
    """Request payload for ``document_create``."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.chunking_strategy is None:
            raise ValueError("chunking_strategy is required")


@dataclass(frozen=True, kw_only=True)
class DocumentUpdateRequest(DocumentWriteRequest):
    """Request payload for ``document_update``."""


@dataclass(frozen=True, kw_only=True)
class DocumentWriteResult(PublicModel):
    """Stable result returned by document create/update operations."""

    status: str
    operation_id: str
    document_id: str
    source_version_id: str
    details: Mapping[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        known = {model_field.name for model_field in fields(cls)}
        extras = {key: values.pop(key) for key in sorted(set(values) - known)}
        if extras:
            details = dict(values.get("details") or {})
            details.update(extras)
            values["details"] = details
        return _read(cls, values)


DocumentCreateResult = DocumentWriteResult
DocumentUpdateResult = DocumentWriteResult


@dataclass(frozen=True, kw_only=True)
class DocumentChunkRequest(PublicModel):
    document_id: str
    chunking_strategy: str | None = None

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("document_id must be non-empty")
        if self.chunking_strategy is not None and self.chunking_strategy not in {
            "paragraph",
            "sentence",
            "semantic",
        }:
            raise ValueError("chunking_strategy must be paragraph, sentence, or semantic")


DocumentChunkResult = DocumentWriteResult


@dataclass(frozen=True, kw_only=True)
class ChunkVersionItem(PublicModel):
    """Public summary of one semantic chunk text version."""

    version_no: int
    id: str | None = None
    logical_chunk_id: str | None = None
    preview: str = ""
    text: str | None = None
    created_at: str | None = None
    current: bool = False
    status: str = "active"
    valid_from: str | None = None
    valid_to: str | None = None
    char_count: int = 0
    text_sha256: str | None = None
    checksum: str | None = None
    comment: str | None = None
    actor: str | None = None
    operation: str | None = None
    previous_version_id: str | None = None
    restored_from_version_id: str | None = None

    def __post_init__(self) -> None:
        _validate_version_no(self.version_no)
        for field_name in ("id", "logical_chunk_id", "previous_version_id", "restored_from_version_id"):
            value = getattr(self, field_name)
            if value is not None:
                _validate_uuid4(value, field_name)
        if self.char_count < 0:
            raise ValueError("char_count must be non-negative")
        if self.status not in {"active", "retired", "deleted"}:
            raise ValueError("status must be active, retired, or deleted")

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        if "current" not in values and "is_current" in values:
            values["current"] = values.pop("is_current")
        # Set-current returns the runtime row, while list returns the public summary.
        known = {model_field.name for model_field in fields(cls)}
        values = {name: values[name] for name in known if name in values}
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class ChunkVersionListRequest(TemporalRequest):
    """Explicit version history of one chunk, read at an optional time pair."""

    chunk_id: str
    include_deleted: bool = False
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_uuid4(self.chunk_id, "chunk_id")
        if not isinstance(self.include_deleted, bool):
            raise ValueError("include_deleted must be a boolean")
        _validate_version_bounds(self.limit, self.offset)


@dataclass(frozen=True, kw_only=True)
class ChunkVersionListResult(TemporalResult):
    """Version history plus the resolved pair it was read at."""

    chunk_id: str
    items: tuple[ChunkVersionItem, ...] = ()
    total: int = 0
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_uuid4(self.chunk_id, "chunk_id")
        _validate_version_bounds(self.limit, self.offset)
        if self.total < 0:
            raise ValueError("total must be non-negative")

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        result["items"] = [item.to_payload() for item in self.items]
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        values["items"] = tuple(
            item if isinstance(item, ChunkVersionItem) else ChunkVersionItem.from_payload(item)
            for item in values.get("items", ())
        )
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class ChunkVersionSetCurrentRequest(PublicModel):
    chunk_id: str
    version_no: int
    comment: str | None = None
    actor: str | None = None

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")
        _validate_version_no(self.version_no)


@dataclass(frozen=True, kw_only=True)
class ChunkVersionSetCurrentResult(PublicModel):
    chunk_id: str
    outcome: str
    version: ChunkVersionItem | None = None

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        if self.version is not None:
            result["version"] = self.version.to_payload()
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        version = values.get("version")
        if isinstance(version, Mapping):
            values["version"] = ChunkVersionItem.from_payload(version)
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class ChunkHistoryRequest(ChunkVersionListRequest):
    """Request payload for ``chunk_history``."""


ChunkHistoryResult = ChunkVersionListResult


@dataclass(frozen=True, kw_only=True)
class ChunkVersionGetRequest(PublicModel):
    chunk_id: str
    version_no: int | None = None
    current: bool = False
    include_text: bool = True

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")
        if self.version_no is not None:
            _validate_version_no(self.version_no)
        if not isinstance(self.current, bool):
            raise ValueError("current must be a boolean")
        if not isinstance(self.include_text, bool):
            raise ValueError("include_text must be a boolean")


@dataclass(frozen=True, kw_only=True)
class ChunkVersionGetResult(PublicModel):
    chunk_id: str
    version: ChunkVersionItem

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        result["version"] = self.version.to_payload()
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        if isinstance(values.get("version"), Mapping):
            values["version"] = ChunkVersionItem.from_payload(values["version"])
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class ChunkVersionTextMutationRequest(PublicModel):
    chunk_id: str
    text: str
    comment: str | None = None
    actor: str | None = None
    expected_current_version: int | None = None
    operation_id: str | None = None

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        if self.expected_current_version is not None:
            _validate_version_no(self.expected_current_version)
        if self.operation_id is not None:
            _validate_uuid4(self.operation_id, "operation_id")


ChunkVersionAddRequest = ChunkVersionTextMutationRequest
ChunkVersionUpdateRequest = ChunkVersionTextMutationRequest
ChunkVersionAddResult = ChunkVersionSetCurrentResult
ChunkVersionUpdateResult = ChunkVersionSetCurrentResult


@dataclass(frozen=True, kw_only=True)
class ChunkVersionRestoreRequest(PublicModel):
    chunk_id: str
    version_no: int
    comment: str | None = None
    actor: str | None = None
    expected_current_version: int | None = None
    operation_id: str | None = None

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")
        _validate_version_no(self.version_no)
        if self.expected_current_version is not None:
            _validate_version_no(self.expected_current_version)
        if self.operation_id is not None:
            _validate_uuid4(self.operation_id, "operation_id")


ChunkVersionRestoreResult = ChunkVersionSetCurrentResult


@dataclass(frozen=True, kw_only=True)
class ChunkVersionRetireRequest(PublicModel):
    chunk_id: str
    version_no: int
    replacement_version_no: int | None = None
    comment: str | None = None
    actor: str | None = None

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")
        _validate_version_no(self.version_no)
        if self.replacement_version_no is not None:
            _validate_version_no(self.replacement_version_no)


@dataclass(frozen=True, kw_only=True)
class ChunkVersionRetireResult(PublicModel):
    chunk_id: str
    outcome: str
    retired_version_no: int
    current_version_no: int | None = None

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")
        _validate_version_no(self.retired_version_no)
        if self.current_version_no is not None:
            _validate_version_no(self.current_version_no)


@dataclass(frozen=True, kw_only=True)
class ChunkVersionDiffRequest(PublicModel):
    chunk_id: str
    from_version_no: int
    to_version_no: int
    context_lines: int = 3

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")
        _validate_version_no(self.from_version_no)
        _validate_version_no(self.to_version_no)
        if isinstance(self.context_lines, bool) or not isinstance(self.context_lines, int) or not 0 <= self.context_lines <= 20:
            raise ValueError("context_lines must be an integer between 0 and 20")


@dataclass(frozen=True, kw_only=True)
class ChunkVersionDiffResult(PublicModel):
    chunk_id: str
    from_version: ChunkVersionItem
    to_version: ChunkVersionItem
    diff: tuple[str, ...] = ()
    changed: bool = False

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        result["from_version"] = self.from_version.to_payload()
        result["to_version"] = self.to_version.to_payload()
        result["diff"] = list(self.diff)
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        if isinstance(values.get("from_version"), Mapping):
            values["from_version"] = ChunkVersionItem.from_payload(values["from_version"])
        if isinstance(values.get("to_version"), Mapping):
            values["to_version"] = ChunkVersionItem.from_payload(values["to_version"])
        values["diff"] = tuple(values.get("diff", ()))
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class ChunkVersionDeleteRequest(PublicModel):
    chunk_id: str
    version_no: int

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")
        _validate_version_no(self.version_no)


@dataclass(frozen=True, kw_only=True)
class ChunkVersionDeleteResult(PublicModel):
    chunk_id: str
    outcome: str
    deleted_version_no: int
    current_version_no: int | None = None

    def __post_init__(self) -> None:
        _validate_uuid4(self.chunk_id, "chunk_id")
        _validate_version_no(self.deleted_version_no)
        if self.current_version_no is not None:
            _validate_version_no(self.current_version_no)


@dataclass(frozen=True, kw_only=True)
class DocumentRebindRequest(PublicModel):
    document_id: str
    project: str | None = None
    project_id: str | None = None
    project_description: str | None = None
    document_properties: Mapping[str, Any] | None = None
    chunk_properties: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("document_id must be non-empty")
        if self.project is not None and not self.project.strip():
            raise ValueError("project must be non-empty when supplied")
        if self.project is not None:
            if self.project_id is None or not self.project_id.strip():
                raise ValueError("project_id is required when project is supplied")
            if self.project_description is None or not self.project_description.strip():
                raise ValueError("project_description is required when project is supplied")
        elif self.project_id is not None or self.project_description is not None:
            raise ValueError("project_id and project_description require project")
        if not any(
            value is not None
            for value in (self.project, self.document_properties, self.chunk_properties)
        ):
            raise ValueError("at least one rebind field is required")


@dataclass(frozen=True, kw_only=True)
class DocumentRebindResult(PublicModel):
    outcome: str
    document_id: str
    project: str | None = None
    project_id: str | None = None
    project_description: str | None = None
    document_properties: Mapping[str, Any] | None = None
    chunk_properties: Mapping[str, Any] | None = None
    updated: Mapping[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class DocumentDeleteRequest(PublicModel):
    document_id: str
    version_token: str

    def __post_init__(self) -> None:
        if not self.document_id.strip() or not self.version_token.strip():
            raise ValueError("document_id and version_token must be non-empty")


@dataclass(frozen=True, kw_only=True)
class DocumentDeleteResult(PublicModel):
    outcome: str
    document_id: str


@dataclass(frozen=True, kw_only=True)
class EntityListRequest(TemporalRequest):
    """Ordinary, version-transparent listing at an optional time pair."""

    entity_type: str
    fields: tuple[str, ...] | None = None
    filters: Mapping[str, Any] | None = None
    limit: int = 50
    offset: int = 0
    show_deleted: bool = False


@dataclass(frozen=True, kw_only=True)
class EntityGetRequest(PublicModel):
    entity_type: str
    entity_id: str
    fields: tuple[str, ...] | None = None
    show_deleted: bool = False


@dataclass(frozen=True, kw_only=True)
class EntityIdsRequest(PublicModel):
    entity_type: str
    ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.ids:
            raise ValueError("ids must be non-empty")


@dataclass(frozen=True, kw_only=True)
class EntityReferencesRequest(PublicModel):
    entity_type: str
    entity_id: str


@dataclass(frozen=True, kw_only=True)
class EntityListResult(TemporalResult):
    """Ordinary listing: one resolved visible version per object, never a history."""

    entity_type: str
    items: tuple[Mapping[str, Any], ...] = ()
    limit: int = 50
    offset: int = 0
    total: int = 0
    show_deleted: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        _reject_repeated_objects(self.items, context="entity_list")

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        values["items"] = tuple(values.get("items", ()))
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class EntityGetResult(PublicModel):
    entity_type: str
    id: str
    value: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class EntityLifecycleResult(PublicModel):
    outcome: str
    updated: Mapping[str, int] | None = None
    deleted: Mapping[str, int] | None = None
    blocked: tuple[Mapping[str, Any], ...] = ()
    is_deleted: bool | None = None

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        values["blocked"] = tuple(values.get("blocked", ()))
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class EntityReferencesResult(PublicModel):
    entity_type: str
    id: str
    references: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        values["references"] = tuple(values.get("references", ()))
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class TextReconstructionRequest(PublicModel):
    document_id: str | None = None
    file_id: str | None = None
    source_name: str | None = None
    source_path: str | None = None
    project_id: str | None = None
    metadata_filters: Mapping[str, Any] | None = None
    max_chars: int = 200_000
    limit: int = 10_000
    offset: int = 0

    def __post_init__(self) -> None:
        _validate_reconstruction_text_selectors(
            document_id=self.document_id,
            file_id=self.file_id,
            source_name=self.source_name,
            source_path=self.source_path,
            project_id=self.project_id,
        )
        if not any(
            (
                self.document_id,
                self.file_id,
                self.source_name,
                self.source_path,
                self.project_id,
                self.metadata_filters,
            )
        ):
            raise ValueError("at least one selector is required")
        if self.max_chars < 0:
            raise ValueError("max_chars must be non-negative")
        if self.limit < 1:
            raise ValueError("limit must be positive")
        if self.offset < 0:
            raise ValueError("offset must be non-negative")


@dataclass(frozen=True, kw_only=True)
class ChapterTextGetRequest(TextReconstructionRequest):
    chapter_id: str | None = None
    include_context: bool = False

    def __post_init__(self) -> None:
        _validate_reconstruction_text_selectors(
            document_id=self.document_id,
            file_id=self.file_id,
            source_name=self.source_name,
            source_path=self.source_path,
            project_id=self.project_id,
        )
        if self.chapter_id is not None and not self.chapter_id.strip():
            raise ValueError("chapter_id must be non-empty when supplied")
        if self.chapter_id is not None:
            _validate_reconstruction_bounds(self.max_chars, self.limit, self.offset)
            return
        super().__post_init__()


@dataclass(frozen=True, kw_only=True)
class SourceFileReconstructRequest(TextReconstructionRequest):
    """Request payload for reconstructing stored source text from chunks."""


@dataclass(frozen=True, kw_only=True)
class TextReconstructionResult(PublicModel):
    entity: str
    selector: Mapping[str, Any] = field(default_factory=dict)
    text: str = ""
    preview: str = ""
    body_sha256: str = ""
    char_count: int = 0
    chunk_count: int = 0
    paragraph_count: int = 0
    document_ids: tuple[str, ...] = ()
    chapter_ids: tuple[str, ...] = ()
    source_names: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()
    range_map: tuple[Mapping[str, Any], ...] = ()
    truncated: bool = False
    limit: int = 10_000
    offset: int = 0
    max_chars: int = 200_000
    versioning: Mapping[str, Any] | None = None
    context: Mapping[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        for name in ("document_ids", "chapter_ids", "source_names", "source_paths", "range_map"):
            values[name] = tuple(values.get(name, ()))
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class EntityOwnerTreeRequest(PublicModel):
    entity_id: str
    entity_type: str | None = None
    max_depth: int = 5
    max_children_per_node: int = 200
    include_deleted: bool = False

    def __post_init__(self) -> None:
        if not self.entity_id.strip():
            raise ValueError("entity_id must be non-empty")
        if self.max_depth < 0 or self.max_depth > 20:
            raise ValueError("max_depth must be between 0 and 20")
        if self.max_children_per_node < 1 or self.max_children_per_node > 500:
            raise ValueError("max_children_per_node must be between 1 and 500")


@dataclass(frozen=True, kw_only=True)
class EntityOwnerTreeResult(PublicModel):
    entity_type: str
    id: str
    tree: Mapping[str, Any]
    max_depth: int = 5
    max_children_per_node: int = 200
    include_deleted: bool = False


@dataclass(frozen=True, kw_only=True)
class SemanticChunkMetadataUpdateRequest(PublicModel):
    chunk_id: str | None = None
    chunk_ids: tuple[str, ...] | None = None
    filters: Mapping[str, Any] | None = None
    updates: Mapping[str, Any] | None = None
    items: tuple[Mapping[str, Any], ...] | None = None
    limit: int = 100
    offset: int = 0
    include_deleted: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        selector_count = sum(
            value is not None for value in (self.chunk_id, self.chunk_ids, self.filters)
        )
        if self.items is not None:
            if self.updates is not None or selector_count:
                raise ValueError("items cannot be combined with selectors or updates")
            if not self.items:
                raise ValueError("items must not be empty")
        else:
            if self.updates is None:
                raise ValueError("updates is required without items")
            if selector_count != 1:
                raise ValueError("select exactly one of chunk_id, chunk_ids, or filters")
        if self.chunk_id is not None and not self.chunk_id.strip():
            raise ValueError("chunk_id must be non-empty")
        if self.chunk_ids is not None and not self.chunk_ids:
            raise ValueError("chunk_ids must not be empty")
        if self.limit < 1 or self.limit > 10000:
            raise ValueError("limit must be between 1 and 10000")
        if self.offset < 0:
            raise ValueError("offset must be non-negative")


@dataclass(frozen=True, kw_only=True)
class SemanticChunkMetadataUpdateResult(PublicModel):
    outcome: str
    requested: int
    matched: int
    updated: int
    items: tuple[Mapping[str, Any], ...] = ()
    dry_run: bool = False

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        values["items"] = tuple(values.get("items", ()))
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class ProcessingStatusRequest(PublicModel):
    operation_id: str
    document_id: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_id.strip() or (
            self.document_id is not None and not self.document_id.strip()
        ):
            raise ValueError("operation_id and document_id must be non-empty when supplied")


@dataclass(frozen=True, kw_only=True)
class ServerError(PublicModel):
    """Structured error data returned by a server command."""

    code: str
    message: str
    details: Mapping[str, Any] | None = None
    type: str | None = None

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        known = {model_field.name for model_field in fields(cls)}
        extras = {key: values.pop(key) for key in sorted(set(values) - known)}
        if extras:
            details = dict(values.get("details") or {})
            details.update(extras)
            values["details"] = details
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class ProcessingStatusResult(PublicModel):
    operation_id: str
    status: str
    progress: int | float | None = None
    timestamps: Mapping[str, Any] = field(default_factory=dict)
    document_reference: str | None = None
    version_reference: str | None = None
    failure: ServerError | Mapping[str, Any] | None = None
    document_id: str | None = None
    requested_document_id: str | None = None

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        if isinstance(self.failure, ServerError):
            result["failure"] = self.failure.to_payload()
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        failure = values.get("failure")
        if isinstance(failure, Mapping):
            values["failure"] = ServerError.from_payload(failure)
        return _read(cls, values)


def _validate_reconstruction_bounds(max_chars: int, limit: int, offset: int) -> None:
    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
    if limit < 1:
        raise ValueError("limit must be positive")
    if offset < 0:
        raise ValueError("offset must be non-negative")


def _validate_uuid4(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a UUID4")
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        raise ValueError(f"{field_name} must be a UUID4") from None
    if parsed.version != 4:
        raise ValueError(f"{field_name} must be a UUID4")


def _validate_version_no(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("version_no must be a positive integer")


def _validate_version_bounds(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > 10_000_000:
        raise ValueError("offset must be between 0 and 10000000")


def _validate_reconstruction_text_selectors(
    *,
    document_id: str | None,
    file_id: str | None,
    source_name: str | None,
    source_path: str | None,
    project_id: str | None,
) -> None:
    values = {
        "document_id": document_id,
        "file_id": file_id,
        "source_name": source_name,
        "source_path": source_path,
        "project_id": project_id,
    }
    for field_name, value in values.items():
        if value is not None and not value.strip():
            raise ValueError(f"{field_name} must be non-empty when supplied")


@dataclass(frozen=True, kw_only=True)
class RetrievalRequest(PublicModel):
    source_version: int | None = None

    def __post_init__(self) -> None:
        if self.source_version is not None and (
            isinstance(self.source_version, bool) or self.source_version <= 0
        ):
            raise ValueError("source_version must be a positive integer")


@dataclass(frozen=True, kw_only=True)
class DocumentGetRequest(RetrievalRequest):
    document_id: str


@dataclass(frozen=True, kw_only=True)
class ChapterGetRequest(RetrievalRequest):
    chapter_id: str


@dataclass(frozen=True, kw_only=True)
class ParagraphGetRequest(RetrievalRequest):
    paragraph_id: str


@dataclass(frozen=True, kw_only=True)
class ParagraphGetByNumberRequest(RetrievalRequest):
    document_id: str
    paragraph_number: int

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.document_id.strip():
            raise ValueError("document_id must be non-empty")
        if isinstance(self.paragraph_number, bool) or self.paragraph_number < 1:
            raise ValueError("paragraph_number must be a positive integer")


@dataclass(frozen=True, kw_only=True)
class RetrievalResult(PublicModel):
    entity: str
    identifier: str
    source_version: int | None = None
    value: Any = None


DocumentGetResult = RetrievalResult
ChapterGetResult = RetrievalResult
ParagraphGetResult = RetrievalResult


@dataclass(frozen=True, kw_only=True)
class ParagraphGetByNumberResult(PublicModel):
    entity: str
    document_id: str
    paragraph_number: int
    identifier: str | None = None
    source_version: int | None = None
    text: str | None = None
    value: Any = None


@dataclass(frozen=True, kw_only=True)
class RankedSearchHit(PublicModel):
    chunk_id: str
    chunk: Mapping[str, Any]
    bm25_score: float | None = None
    semantic_score: float | None = None
    hybrid_score: float | None = None
    rank: int = 0
    matched_fields: tuple[str, ...] | None = None
    highlights: Mapping[str, tuple[str, ...]] | None = None
    search_metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("bm25_score", "semantic_score", "hybrid_score"):
            score = getattr(self, name)
            if score is not None and not 0 <= score <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.rank < 0:
            raise ValueError("rank must be non-negative")


@dataclass(frozen=True, kw_only=True)
class SearchResult(TemporalResult):
    """ChunkQuery search result plus the resolved pair its ranking used."""

    status: str
    results: tuple[RankedSearchHit, ...] = ()
    total_results: int | None = None
    search_time: float | None = None
    query_time: float | None = None
    metadata: Mapping[str, Any] | None = None
    error: ServerError | Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _reject_repeated_objects(
            ({"chunk_id": hit.chunk_id} for hit in self.results),
            context="chunk_query_search",
        )

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        result["results"] = [hit.to_payload() for hit in self.results]
        if isinstance(self.error, ServerError):
            result["error"] = self.error.to_payload()
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        nested = values.pop("data", None)
        if isinstance(nested, Mapping):
            values = {**dict(nested), **values}
        values.setdefault("status", "success")
        values["results"] = tuple(
            RankedSearchHit.from_payload(item) for item in values.get("results", ())
        )
        if isinstance(values.get("error"), Mapping):
            values["error"] = ServerError.from_payload(values["error"])
        return _read(cls, values)


@dataclass(frozen=True, kw_only=True)
class OperationState(PublicModel):
    operation_id: str
    status: str
    document_id: str | None = None
    source_version_id: str | None = None
    message: str | None = None
    error: ServerError | Mapping[str, Any] | None = None

    def to_params(self) -> dict[str, Any]:
        result = _payload(self)
        if isinstance(self.error, ServerError):
            result["error"] = self.error.to_payload()
        return result

    @classmethod
    def from_payload(cls, payload: Payload) -> Self:
        values = dict(payload)
        if isinstance(values.get("error"), Mapping):
            values["error"] = ServerError.from_payload(values["error"])
        return _read(cls, values)


__all__ = [
    "ChapterGetRequest", "ChapterGetResult", "ChapterTextGetRequest", "ChunkQuery",
    "ChunkHistoryRequest", "ChunkHistoryResult", "ChunkVersionAddRequest",
    "ChunkVersionAddResult", "ChunkVersionDeleteRequest", "ChunkVersionDeleteResult",
    "ChunkVersionDiffRequest", "ChunkVersionDiffResult", "ChunkVersionGetRequest",
    "ChunkVersionGetResult", "ChunkVersionItem", "ChunkVersionListRequest",
    "ChunkVersionListResult", "ChunkVersionRestoreRequest", "ChunkVersionRestoreResult",
    "ChunkVersionRetireRequest", "ChunkVersionRetireResult", "ChunkVersionSetCurrentRequest",
    "ChunkVersionSetCurrentResult", "ChunkVersionTextMutationRequest",
    "ChunkVersionUpdateRequest", "ChunkVersionUpdateResult",
    "DocumentCreateRequest",
    "DocumentCreateResult", "DocumentChunkRequest", "DocumentChunkResult",
    "DocumentDeleteRequest", "DocumentDeleteResult", "DocumentGetRequest", "DocumentGetResult",
    "DocumentRebindRequest", "DocumentRebindResult", "DocumentUpdateRequest", "DocumentUpdateResult",
    "DocumentWriteRequest", "DocumentWriteResult", "EntityGetRequest", "EntityGetResult",
    "EntityIdsRequest", "EntityLifecycleResult", "EntityListRequest", "EntityListResult",
    "EntityOwnerTreeRequest", "EntityOwnerTreeResult", "EntityReferencesRequest",
    "EntityReferencesResult", "OperationState",
    "ParagraphGetByNumberRequest", "ParagraphGetByNumberResult", "ParagraphGetRequest",
    "ParagraphGetResult", "ProcessingStatusRequest", "ProcessingStatusResult", "RankedSearchHit",
    "RetrievalRequest", "RetrievalResult", "SearchResult", "ServerError",
    "TEMPORAL_BOUND_FIELDS", "TemporalCursor", "TemporalPayload", "TemporalRequest",
    "TemporalResult", "canonical_query_id", "temporal_bounds", "with_temporal_bounds",
    "SemanticChunkMetadataUpdateRequest", "SemanticChunkMetadataUpdateResult",
    "SourceFileReconstructRequest", "TextReconstructionRequest", "TextReconstructionResult",
]
