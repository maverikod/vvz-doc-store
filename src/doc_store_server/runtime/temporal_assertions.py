"""Append-only write service over the shared ``TemporalAssertion`` identity.

Every temporal mutation of a registered object is an *append*. Nothing in this
module ever rewrites an accepted row: the database refuses it (the BEFORE
UPDATE trigger installed by migration ``0018_bitemporal_assertions`` raises
``restrict_violation`` on any change to accepted evidence), and the semantics
below never need it.

How each mutation is represented:

* **create** appends a ``payload`` assertion with no predecessor;
* **supersession** appends a new assertion whose ``supersedes_assertion_id``
  names the corrected one. The known-time end of the superseded assertion is
  *derived* from the successor's ``recorded_at`` and is deliberately never
  written back;
* **interval closure** appends an ``interval_boundary`` copy of an open-ended
  assertion that carries the new ``effective_until``;
* **deletion** appends a ``deletion`` assertion covering the removed effective
  window and superseding the assertion it removes. Earlier effective times and
  earlier known times keep resolving to the original assertion, so nothing that
  was ever visible stops being visible as-of the time it was true;
* **restoration** appends a ``restoration`` assertion superseding the deletion.
  The deletion row is neither rewritten nor removed, so a query as-of a known
  time between deletion and restoration still sees the object as deleted;
* **partial interval replacement** globally supersedes the original at the
  correction's ``recorded_at`` and appends only the non-empty segments: a
  left original-copy, the corrected overlap, and a right original-copy. The
  three appended segments are non-overlapping and together cover exactly the
  original interval.

Effective time is the half-open interval ``[effective_from, effective_until)``
with ``NULL`` meaning open-ended. Transaction time is always the database's
``now()`` for the appending transaction, never a caller-supplied clock, so
accepted order and transaction-time order agree.

The service holds no selected-current pointer: temporal authority is the
assertion history alone. Resolution as-of ``act_date``/``known_at``, hard-delete
governance and deletion-order policy live outside this module.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
import os
from types import MappingProxyType
from typing import Any, Iterator
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from doc_store_server.db.health import database_url_from_config
from doc_store_server.db.schema import ASSERTION_KINDS, REGISTRY_KIND_LOCATORS


ASSERTION_TABLE = "temporal_assertions"
REGISTRY_TABLE = "entity_uuid_registry"

OBJECT_NOT_REGISTERED = "OBJECT_NOT_REGISTERED"
ASSERTION_NOT_FOUND = "ASSERTION_NOT_FOUND"
ASSERTION_ALREADY_SUPERSEDED = "ASSERTION_ALREADY_SUPERSEDED"
INVALID_EFFECTIVE_INTERVAL = "INVALID_EFFECTIVE_INTERVAL"
AMBIGUOUS_EFFECTIVE_INTERVAL = "AMBIGUOUS_EFFECTIVE_INTERVAL"
DISJOINT_REPLACEMENT_WINDOW = "DISJOINT_REPLACEMENT_WINDOW"
NOT_A_DELETION_ASSERTION = "NOT_A_DELETION_ASSERTION"

_COLUMNS = (
    "assertion_id, object_id, payload_family, assertion_kind, revision_no, "
    "supersedes_assertion_id, effective_from, effective_until, recorded_at, "
    "accepted_by, accepted_via, acceptance_operation_id, acceptance_comment"
)

_TABLE_KINDS: Mapping[str, str] = MappingProxyType(
    {table: kind for kind, table in REGISTRY_KIND_LOCATORS.items()}
)

# Parent registry kind -> ((child table, foreign-key column), ...). Only tables
# that exist in the schema appear here; unregistered children are dropped after
# expansion, because a deletion assertion may only name a registered object.
DESCENDANT_EDGES: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        "project": (("projects", "owner_id"), ("files", "owner_id"), ("documents", "owner_id")),
        "file": (("files", "owner_id"), ("documents", "owner_id")),
        "document": (
            ("chapters", "document_id"),
            ("paragraphs", "document_id"),
            ("semantic_chunks", "document_id"),
        ),
        "chapter": (("paragraphs", "chapter_id"), ("semantic_chunks", "chapter_id")),
        "paragraph": (("semantic_chunks", "paragraph_id"),),
    }
)

#: Copies family payload rows keyed by an assertion UUID onto a newly appended
#: assertion, inside the same transaction: ``(connection, source, target)``.
#: This is the seam a payload family uses to duplicate an original-copy segment;
#: the assertion table itself stores no payload.
PayloadCopier = Callable[[Connection, UUID, UUID], None]

#: Writes the corrected payload of a replacement segment inside the same
#: transaction: ``(connection, corrected_assertion_id)``.
PayloadWriter = Callable[[Connection, UUID], None]


class _Unset:
    """Sentinel distinguishing "leave unchanged" from an explicit ``None``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET = _Unset()


class TemporalAssertionError(RuntimeError):
    """Raised when an append-only temporal assertion write cannot be performed."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True)
class Acceptance:
    """Provenance recorded with an appended assertion."""

    accepted_by: str | None = None
    accepted_via: str | None = None
    operation_id: UUID | None = None
    comment: str | None = None

    @classmethod
    def build(
        cls,
        accepted_by: str | None,
        accepted_via: str | None,
        operation_id: str | UUID | None,
        comment: str | None,
    ) -> Acceptance:
        return cls(
            accepted_by=_optional_text(accepted_by, "accepted_by", 255),
            accepted_via=_optional_text(accepted_via, "accepted_via", 64),
            operation_id=_optional_uuid(operation_id, "operation_id"),
            comment=_optional_text(comment, "comment", None),
        )

    def as_params(self) -> dict[str, Any]:
        return {
            "accepted_by": self.accepted_by,
            "accepted_via": self.accepted_via,
            "acceptance_operation_id": self.operation_id,
            "acceptance_comment": self.comment,
        }


@dataclass(frozen=True)
class _Interval:
    """Half-open effective interval; ``until is None`` means open-ended."""

    start: datetime
    until: datetime | None

    def overlaps(self, other: _Interval) -> bool:
        return _before(self.start, other.until) and _before(other.start, self.until)

    def clipped_to(self, other: _Interval) -> _Interval | None:
        start = max(self.start, other.start)
        until = _earliest(self.until, other.until)
        if not _before(start, until):
            return None
        return _Interval(start, until)


class TemporalAssertionService:
    """Append temporal assertions; never rewrite one."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    # ------------------------------------------------------------------ reads

    def history(self, *, object_id: str | UUID) -> dict[str, Any]:
        """Return every assertion ever appended for one registered object."""

        target = _uuid(object_id, "object_id")
        with self._transaction() as connection:
            rows = connection.execute(
                text(
                    f"SELECT {_COLUMNS} FROM {ASSERTION_TABLE} "
                    "WHERE object_id = :object_id ORDER BY revision_no ASC"
                ),
                {"object_id": target},
            ).mappings().all()
        items = [_assertion_payload(row) for row in rows]
        return {"object_id": str(target), "items": items, "total": len(items)}

    def accepted(self, *, object_id: str | UUID) -> dict[str, Any]:
        """Return the assertions no later assertion has superseded yet."""

        target = _uuid(object_id, "object_id")
        with self._transaction() as connection:
            rows = self._accepted_rows(connection, target)
        items = [_assertion_payload(row) for row in rows]
        return {"object_id": str(target), "items": items, "total": len(items)}

    # ----------------------------------------------------------------- writes

    def create_assertion(
        self,
        *,
        object_id: str | UUID,
        effective_from: str | datetime,
        effective_until: str | datetime | None = None,
        payload_family: str | None = None,
        assertion_kind: str = "payload",
        accepted_by: str | None = None,
        accepted_via: str | None = None,
        operation_id: str | UUID | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Append a first-hand payload assertion with no predecessor."""

        target = _uuid(object_id, "object_id")
        interval = _interval(effective_from, effective_until)
        kind = _assertion_kind(assertion_kind)
        acceptance = Acceptance.build(accepted_by, accepted_via, operation_id, comment)
        with self._transaction() as connection:
            registry = self._locked_object(connection, target)
            family = _payload_family(payload_family, registry)
            row = self._append(
                connection,
                object_id=target,
                payload_family=family,
                assertion_kind=kind,
                interval=interval,
                supersedes=None,
                acceptance=acceptance,
            )
        return {"outcome": "appended", "assertion": _assertion_payload(row)}

    def supersede_assertion(
        self,
        *,
        assertion_id: str | UUID,
        effective_from: str | datetime | None = None,
        effective_until: str | datetime | None | _Unset = UNSET,
        assertion_kind: str = "supersession",
        payload_copier: PayloadCopier | None = None,
        accepted_by: str | None = None,
        accepted_via: str | None = None,
        operation_id: str | UUID | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Correct one accepted assertion by appending its successor.

        The prior row keeps every value it was accepted with; its known time
        simply ends at the successor's ``recorded_at``.
        """

        prior_id = _uuid(assertion_id, "assertion_id")
        kind = _assertion_kind(assertion_kind)
        acceptance = Acceptance.build(accepted_by, accepted_via, operation_id, comment)
        with self._transaction() as connection:
            prior = self._acceptable_prior(connection, prior_id)
            self._locked_object(connection, prior["object_id"])
            interval = _interval(
                effective_from if effective_from is not None else prior["effective_from"],
                prior["effective_until"] if isinstance(effective_until, _Unset) else effective_until,
            )
            row = self._append(
                connection,
                object_id=prior["object_id"],
                payload_family=str(prior["payload_family"]),
                assertion_kind=kind,
                interval=interval,
                supersedes=prior_id,
                acceptance=acceptance,
                ignore=(prior_id,),
            )
            _copy_payload(payload_copier, connection, prior_id, row["assertion_id"])
        return {
            "outcome": "superseded",
            "superseded_assertion_id": str(prior_id),
            "assertion": _assertion_payload(row),
        }

    def close_interval(
        self,
        *,
        assertion_id: str | UUID,
        effective_until: str | datetime,
        payload_copier: PayloadCopier | None = None,
        accepted_by: str | None = None,
        accepted_via: str | None = None,
        operation_id: str | UUID | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Append an interval-boundary assertion ending an accepted interval."""

        return self.supersede_assertion(
            assertion_id=assertion_id,
            effective_until=effective_until,
            assertion_kind="interval_boundary",
            payload_copier=payload_copier,
            accepted_by=accepted_by,
            accepted_via=accepted_via,
            operation_id=operation_id,
            comment=comment,
        )

    def replace_interval(
        self,
        *,
        assertion_id: str | UUID,
        effective_from: str | datetime,
        effective_until: str | datetime | None = None,
        payload_copier: PayloadCopier | None = None,
        payload_writer: PayloadWriter | None = None,
        accepted_by: str | None = None,
        accepted_via: str | None = None,
        operation_id: str | UUID | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Correct part of an accepted interval, keeping the rest untouched.

        The original is globally superseded at this correction's ``recorded_at``
        and replaced by the non-empty segments among left original-copy,
        corrected overlap and right original-copy. Empty segments are omitted;
        no earlier assertion is rewritten. ``payload_copier`` duplicates the
        original payload onto the left and right copies and ``payload_writer``
        stores the corrected payload, both in this transaction.
        """

        prior_id = _uuid(assertion_id, "assertion_id")
        window = _interval(effective_from, effective_until)
        acceptance = Acceptance.build(accepted_by, accepted_via, operation_id, comment)
        with self._transaction() as connection:
            prior = self._acceptable_prior(connection, prior_id)
            self._locked_object(connection, prior["object_id"])
            segments = self._split(
                connection,
                prior=prior,
                window=window,
                corrected_kind="supersession",
                acceptance=acceptance,
                payload_copier=payload_copier,
                payload_writer=payload_writer,
            )
        return {
            "outcome": "replaced",
            "superseded_assertion_id": str(prior_id),
            **segments,
        }

    def soft_delete(
        self,
        *,
        object_ids: Sequence[str | UUID],
        include_descendants: bool = False,
        effective_from: str | datetime | None = None,
        effective_until: str | datetime | None | _Unset = UNSET,
        payload_family: str | None = None,
        payload_copier: PayloadCopier | None = None,
        accepted_by: str | None = None,
        accepted_via: str | None = None,
        operation_id: str | UUID | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Append one deletion assertion for every resolved target object.

        The caller supplies an exact UUID list; ``include_descendants`` adds the
        registered descendant closure of each root. The complete target set is
        resolved before anything is written, and the whole batch is one
        transaction. Prior payload evidence is never mutated, so every earlier
        effective time and earlier known time still resolves as it did.

        Without an explicit window the deletion covers the target's whole
        accepted interval; a narrower window removes only that part and keeps
        the rest as left and right original-copies. Deleting an interval that is
        already deleted is refused as ambiguous rather than double-recorded.
        """

        roots = [_uuid(item, "object_ids") for item in object_ids]
        acceptance = Acceptance.build(accepted_by, accepted_via, operation_id, comment)
        if not roots:
            return {"outcome": "deleted", "requested": 0, "resolved": 0, "targets": []}
        with self._transaction() as connection:
            registries = {root: self._locked_object(connection, root) for root in roots}
            targets = list(registries)
            if include_descendants:
                for descendant in self._descendants(connection, registries.values()):
                    if descendant not in registries:
                        registries[descendant] = self._locked_object(connection, descendant)
                        targets.append(descendant)
            results = [
                self._delete_one(
                    connection,
                    registry=registries[target],
                    effective_from=effective_from,
                    effective_until=effective_until,
                    payload_family=payload_family,
                    payload_copier=payload_copier,
                    acceptance=acceptance,
                )
                for target in sorted(targets, key=str)
            ]
        return {
            "outcome": "deleted",
            "requested": len(roots),
            "resolved": len(results),
            "targets": results,
        }

    def restore(
        self,
        *,
        deletion_assertion_id: str | UUID,
        payload_copier: PayloadCopier | None = None,
        accepted_by: str | None = None,
        accepted_via: str | None = None,
        operation_id: str | UUID | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Undo a deletion by appending a restoration assertion over it.

        The deletion row is left exactly as it was accepted, so a query as-of a
        known time between the deletion and this restoration still sees the
        object as deleted.
        """

        deletion_id = _uuid(deletion_assertion_id, "deletion_assertion_id")
        acceptance = Acceptance.build(accepted_by, accepted_via, operation_id, comment)
        with self._transaction() as connection:
            deletion = self._acceptable_prior(connection, deletion_id)
            if str(deletion["assertion_kind"]) != "deletion":
                raise TemporalAssertionError(
                    NOT_A_DELETION_ASSERTION,
                    "restoration must supersede a deletion assertion",
                    {
                        "assertion_id": str(deletion_id),
                        "assertion_kind": str(deletion["assertion_kind"]),
                    },
                )
            self._locked_object(connection, deletion["object_id"])
            row = self._append(
                connection,
                object_id=deletion["object_id"],
                payload_family=str(deletion["payload_family"]),
                assertion_kind="restoration",
                interval=_Interval(deletion["effective_from"], deletion["effective_until"]),
                supersedes=deletion_id,
                acceptance=acceptance,
                ignore=(deletion_id,),
            )
            source = deletion["supersedes_assertion_id"] or deletion_id
            _copy_payload(payload_copier, connection, source, row["assertion_id"])
        return {
            "outcome": "restored",
            "restored_deletion_id": str(deletion_id),
            "assertion": _assertion_payload(row),
        }

    # -------------------------------------------------------------- internals

    def _delete_one(
        self,
        connection: Connection,
        *,
        registry: Mapping[str, Any],
        effective_from: str | datetime | None,
        effective_until: str | datetime | None | _Unset,
        payload_family: str | None,
        payload_copier: PayloadCopier | None,
        acceptance: Acceptance,
    ) -> dict[str, Any]:
        object_id = UUID(str(registry["entity_id"]))
        family = _payload_family(payload_family, registry)
        prior = self._accepted_in_family(connection, object_id, family)
        if prior is None:
            interval = _interval(
                effective_from if effective_from is not None else _now(connection),
                None if isinstance(effective_until, _Unset) else effective_until,
            )
            row = self._append(
                connection,
                object_id=object_id,
                payload_family=family,
                assertion_kind="deletion",
                interval=interval,
                supersedes=None,
                acceptance=acceptance,
            )
            return {
                "object_id": str(object_id),
                "superseded_assertion_id": None,
                "deletion": _assertion_payload(row),
                "left": None,
                "right": None,
            }
        window = _interval(
            effective_from if effective_from is not None else prior["effective_from"],
            prior["effective_until"] if isinstance(effective_until, _Unset) else effective_until,
        )
        segments = self._split(
            connection,
            prior=prior,
            window=window,
            corrected_kind="deletion",
            acceptance=acceptance,
            payload_copier=payload_copier,
        )
        return {
            "object_id": str(object_id),
            "superseded_assertion_id": str(prior["assertion_id"]),
            "deletion": segments["corrected"],
            "left": segments["left"],
            "right": segments["right"],
        }

    def _split(
        self,
        connection: Connection,
        *,
        prior: Mapping[str, Any],
        window: _Interval,
        corrected_kind: str,
        acceptance: Acceptance,
        payload_copier: PayloadCopier | None,
        payload_writer: PayloadWriter | None = None,
    ) -> dict[str, Any]:
        """Append the non-empty segments that replace ``prior`` over ``window``."""

        prior_id = UUID(str(prior["assertion_id"]))
        original = _Interval(prior["effective_from"], prior["effective_until"])
        corrected = window.clipped_to(original)
        if corrected is None:
            raise TemporalAssertionError(
                DISJOINT_REPLACEMENT_WINDOW,
                "the correction window does not overlap the assertion it corrects",
                {
                    "assertion_id": str(prior_id),
                    "effective_from": _isoformat(window.start),
                    "effective_until": _isoformat(window.until),
                },
            )
        left = (
            _Interval(original.start, corrected.start)
            if original.start < corrected.start
            else None
        )
        right = (
            _Interval(corrected.until, original.until)
            if corrected.until is not None and _before(corrected.until, original.until)
            else None
        )
        appended: dict[str, Any] = {}
        for name, interval, kind in (
            ("left", left, str(prior["assertion_kind"])),
            ("corrected", corrected, corrected_kind),
            ("right", right, str(prior["assertion_kind"])),
        ):
            if interval is None:
                appended[name] = None
                continue
            row = self._append(
                connection,
                object_id=UUID(str(prior["object_id"])),
                payload_family=str(prior["payload_family"]),
                assertion_kind=_assertion_kind(kind),
                interval=interval,
                supersedes=prior_id,
                acceptance=acceptance,
                ignore=(prior_id,),
            )
            if name == "corrected":
                # The corrected segment carries the *new* claim, so the family
                # writes it; a deletion segment carries no payload at all.
                if corrected_kind != "deletion" and payload_writer is not None:
                    payload_writer(connection, UUID(str(row["assertion_id"])))
            else:
                _copy_payload(payload_copier, connection, prior_id, row["assertion_id"])
            appended[name] = _assertion_payload(row)
        return appended

    def _append(
        self,
        connection: Connection,
        *,
        object_id: UUID,
        payload_family: str,
        assertion_kind: str,
        interval: _Interval,
        supersedes: UUID | None,
        acceptance: Acceptance,
        ignore: Sequence[UUID] = (),
    ) -> Mapping[str, Any]:
        self._guard_unambiguous(
            connection,
            object_id=object_id,
            payload_family=payload_family,
            interval=interval,
            ignore=ignore,
        )
        params = {
            "assertion_id": uuid4(),
            "object_id": object_id,
            "payload_family": payload_family,
            "assertion_kind": assertion_kind,
            "supersedes_assertion_id": supersedes,
            "effective_from": interval.start,
            "effective_until": interval.until,
            **acceptance.as_params(),
        }
        return connection.execute(
            text(
                f"INSERT INTO {ASSERTION_TABLE} "
                "(assertion_id, object_id, payload_family, assertion_kind, revision_no, "
                "supersedes_assertion_id, effective_from, effective_until, recorded_at, "
                "accepted_by, accepted_via, acceptance_operation_id, acceptance_comment) "
                "SELECT CAST(:assertion_id AS uuid), :object_id, :payload_family, :assertion_kind, "
                f"COALESCE(MAX(revision_no), 0) + 1, :supersedes_assertion_id, "
                "CAST(:effective_from AS timestamptz), CAST(:effective_until AS timestamptz), "
                "now(), :accepted_by, :accepted_via, "
                "CAST(:acceptance_operation_id AS uuid), :acceptance_comment "
                f"FROM {ASSERTION_TABLE} WHERE object_id = :object_id "
                f"RETURNING {_COLUMNS}"
            ),
            params,
        ).mappings().one()

    def _guard_unambiguous(
        self,
        connection: Connection,
        *,
        object_id: UUID,
        payload_family: str,
        interval: _Interval,
        ignore: Sequence[UUID],
    ) -> None:
        """Refuse a second accepted assertion over one effective instant."""

        clash = connection.execute(
            text(
                f"SELECT assertion_id FROM {ASSERTION_TABLE} AS a "
                "WHERE a.object_id = :object_id AND a.payload_family = :payload_family "
                "AND NOT (a.assertion_id = ANY(CAST(:ignore AS uuid[]))) "
                f"AND NOT EXISTS (SELECT 1 FROM {ASSERTION_TABLE} AS s "
                "WHERE s.supersedes_assertion_id = a.assertion_id) "
                "AND (a.effective_until IS NULL OR a.effective_until > :effective_from) "
                "AND (CAST(:effective_until AS timestamptz) IS NULL "
                "OR a.effective_from < CAST(:effective_until AS timestamptz)) "
                "LIMIT 1"
            ),
            {
                "object_id": object_id,
                "payload_family": payload_family,
                "ignore": [str(item) for item in ignore],
                "effective_from": interval.start,
                "effective_until": interval.until,
            },
        ).scalar_one_or_none()
        if clash is not None:
            raise TemporalAssertionError(
                AMBIGUOUS_EFFECTIVE_INTERVAL,
                "an accepted assertion already covers part of that effective interval",
                {
                    "object_id": str(object_id),
                    "payload_family": payload_family,
                    "conflicting_assertion_id": str(clash),
                    "effective_from": _isoformat(interval.start),
                    "effective_until": _isoformat(interval.until),
                },
            )

    def _locked_object(self, connection: Connection, object_id: UUID) -> Mapping[str, Any]:
        """Lock one registry row so revision order stays monotonic under concurrency.

        Only ``entity_id`` and ``entity_table`` are read: the registry's object
        kind is derived from its table locator, which is the one column the
        materialized table and the ORM declaration agree on.
        """

        row = connection.execute(
            text(
                f"SELECT entity_id, entity_table FROM {REGISTRY_TABLE} "
                "WHERE entity_id = :object_id FOR UPDATE"
            ),
            {"object_id": object_id},
        ).mappings().one_or_none()
        if row is None:
            raise TemporalAssertionError(
                OBJECT_NOT_REGISTERED,
                "no registered object carries that UUID",
                {"object_id": str(object_id)},
            )
        return row

    def _acceptable_prior(self, connection: Connection, assertion_id: UUID) -> Mapping[str, Any]:
        row = connection.execute(
            text(f"SELECT {_COLUMNS} FROM {ASSERTION_TABLE} WHERE assertion_id = :assertion_id"),
            {"assertion_id": assertion_id},
        ).mappings().one_or_none()
        if row is None:
            raise TemporalAssertionError(
                ASSERTION_NOT_FOUND,
                "no assertion carries that UUID",
                {"assertion_id": str(assertion_id)},
            )
        successor = connection.execute(
            text(
                f"SELECT assertion_id FROM {ASSERTION_TABLE} "
                "WHERE supersedes_assertion_id = :assertion_id LIMIT 1"
            ),
            {"assertion_id": assertion_id},
        ).scalar_one_or_none()
        if successor is not None:
            raise TemporalAssertionError(
                ASSERTION_ALREADY_SUPERSEDED,
                "that assertion has already been superseded; correct its successor instead",
                {"assertion_id": str(assertion_id), "superseded_by": str(successor)},
            )
        return row

    def _accepted_rows(self, connection: Connection, object_id: UUID) -> Sequence[Mapping[str, Any]]:
        return connection.execute(
            text(
                f"SELECT {_COLUMNS} FROM {ASSERTION_TABLE} AS a "
                "WHERE a.object_id = :object_id "
                f"AND NOT EXISTS (SELECT 1 FROM {ASSERTION_TABLE} AS s "
                "WHERE s.supersedes_assertion_id = a.assertion_id) "
                "ORDER BY a.effective_from ASC, a.revision_no ASC"
            ),
            {"object_id": object_id},
        ).mappings().all()

    def _accepted_in_family(
        self,
        connection: Connection,
        object_id: UUID,
        payload_family: str,
    ) -> Mapping[str, Any] | None:
        rows = [
            row
            for row in self._accepted_rows(connection, object_id)
            if str(row["payload_family"]) == payload_family
            and str(row["assertion_kind"]) != "deletion"
        ]
        if len(rows) > 1:
            raise TemporalAssertionError(
                AMBIGUOUS_EFFECTIVE_INTERVAL,
                "several accepted assertions describe that object; delete an explicit interval",
                {
                    "object_id": str(object_id),
                    "payload_family": payload_family,
                    "accepted_assertion_ids": [str(row["assertion_id"]) for row in rows],
                },
            )
        return rows[0] if rows else None

    def _descendants(
        self,
        connection: Connection,
        registries: Iterable[Mapping[str, Any]],
    ) -> list[UUID]:
        """Resolve the registered descendant closure of the given roots."""

        seen = {UUID(str(row["entity_id"])): _registry_kind(row) for row in registries}
        frontier = list(seen.items())
        found: set[UUID] = set()
        while frontier:
            next_frontier: list[tuple[UUID, str]] = []
            for parent_id, kind in frontier:
                for child_id, child_kind in self._children(connection, parent_id, kind):
                    if child_id in seen:
                        continue
                    seen[child_id] = child_kind
                    found.add(child_id)
                    next_frontier.append((child_id, child_kind))
            frontier = next_frontier
        if not found:
            return []
        registered = connection.execute(
            text(
                f"SELECT entity_id FROM {REGISTRY_TABLE} "
                "WHERE entity_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [str(item) for item in found]},
        ).scalars().all()
        return [UUID(str(item)) for item in registered]

    @staticmethod
    def _children(
        connection: Connection,
        parent_id: UUID,
        kind: str,
    ) -> list[tuple[UUID, str]]:
        children: list[tuple[UUID, str]] = []
        for table, column in DESCENDANT_EDGES.get(kind, ()):
            rows = connection.execute(
                text(f"SELECT id FROM {table} WHERE {column} = :parent_id"),
                {"parent_id": parent_id},
            ).scalars().all()
            children.extend((UUID(str(row)), _TABLE_KINDS[table]) for row in rows)
        if kind == "project":
            rows = connection.execute(
                text("SELECT id FROM documents WHERE block_meta ->> 'project_id' = :parent_id"),
                {"parent_id": str(parent_id)},
            ).scalars().all()
            children.extend((UUID(str(row)), "document") for row in rows)
        return [(child, child_kind) for child, child_kind in children if child != parent_id]

    def _engine(self) -> Any:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        return create_engine(self._database_url, pool_pre_ping=True)

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        engine = self._engine()
        try:
            with engine.begin() as connection:
                yield connection
        finally:
            engine.dispose()


def installed_temporal_assertion_service(
    config: Mapping[str, Any] | None = None,
) -> TemporalAssertionService | None:
    database_url = (
        database_url_from_config(config or {})
        or os.getenv("DOC_STORE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
    )
    return TemporalAssertionService(database_url) if database_url else None


def _copy_payload(
    payload_copier: PayloadCopier | None,
    connection: Connection,
    source: Any,
    target: Any,
) -> None:
    if payload_copier is None:
        return
    payload_copier(connection, UUID(str(source)), UUID(str(target)))


def _now(connection: Connection) -> datetime:
    return connection.execute(text("SELECT now()")).scalar_one()


def _interval(
    effective_from: str | datetime,
    effective_until: str | datetime | None,
) -> _Interval:
    start = _instant(effective_from, "effective_from")
    until = _optional_instant(effective_until, "effective_until")
    if until is not None and until <= start:
        raise TemporalAssertionError(
            INVALID_EFFECTIVE_INTERVAL,
            "effective_until must be later than effective_from",
            {"effective_from": _isoformat(start), "effective_until": _isoformat(until)},
        )
    return _Interval(start, until)


def _instant(value: str | datetime, field_name: str) -> datetime:
    parsed = _optional_instant(value, field_name)
    if parsed is None:
        raise TemporalAssertionError(
            INVALID_EFFECTIVE_INTERVAL,
            f"{field_name} is required",
            {"field": field_name},
        )
    return parsed


def _optional_instant(value: str | datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise TemporalAssertionError(
                INVALID_EFFECTIVE_INTERVAL,
                f"{field_name} must be an ISO-8601 instant",
                {"field": field_name, "value": value},
            ) from exc
    else:
        raise TemporalAssertionError(
            INVALID_EFFECTIVE_INTERVAL,
            f"{field_name} must be an ISO-8601 instant or a datetime",
            {"field": field_name},
        )
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _before(left: datetime | None, right: datetime | None) -> bool:
    """Compare instants where ``None`` on the right means "open-ended"."""

    if right is None:
        return True
    if left is None:
        return False
    return left < right


def _earliest(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _registry_kind(registry: Mapping[str, Any]) -> str:
    """Derive the registered object kind from its typed-table locator."""

    locator = str(registry["entity_table"])
    return _TABLE_KINDS.get(locator, locator)


def _payload_family(payload_family: str | None, registry: Mapping[str, Any]) -> str:
    if payload_family is None:
        return _registry_kind(registry)
    family = payload_family.strip()
    if not family or len(family) > 64:
        raise ValueError("payload_family must be a non-empty string of at most 64 characters")
    return family


def _assertion_kind(assertion_kind: str) -> str:
    if assertion_kind not in ASSERTION_KINDS:
        raise ValueError(f"unsupported assertion_kind: {assertion_kind}")
    return assertion_kind


def _uuid(value: str | UUID, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _optional_uuid(value: str | UUID | None, field_name: str) -> UUID | None:
    return None if value is None else _uuid(value, field_name)


def _optional_text(value: str | None, field_name: str, max_length: int | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return value


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _assertion_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in tuple(result.items()):
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, date):
            result[key] = value.isoformat()
    return result


__all__ = [
    "AMBIGUOUS_EFFECTIVE_INTERVAL",
    "ASSERTION_ALREADY_SUPERSEDED",
    "ASSERTION_NOT_FOUND",
    "Acceptance",
    "DISJOINT_REPLACEMENT_WINDOW",
    "INVALID_EFFECTIVE_INTERVAL",
    "NOT_A_DELETION_ASSERTION",
    "OBJECT_NOT_REGISTERED",
    "PayloadCopier",
    "PayloadWriter",
    "TemporalAssertionError",
    "TemporalAssertionService",
    "UNSET",
    "installed_temporal_assertion_service",
]
