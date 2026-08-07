"""Focused regressions for the ObjectRegistry contract: identity, locator, allocation.

Covers four properties that G-001 owns:

* one registered UUID resolves to exactly one typed-table locator, without the
  caller supplying an object type ({ss45}, {vi72});
* every registry row carries an allowlisted kind and its concrete locator, and
  no parallel registry exists ({dl1d}, {vq2e});
* registration rejects an already registered UUID instead of upserting it or
  reassigning its locator ({b2v7});
* migration 0005 aborts before any DDL when legacy rows already claim one UUID
  from two typed tables, and every table it registers stays consistent with the
  allowlist and with the lookup boundary ({tj7i}).

Everything here is offline: the migration is rendered to SQL with alembic's
as_sql mode and the lookup boundary is driven through a stub connection, so no
PostgreSQL instance is required.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from doc_store_server.db.schema import (
    REGISTRY_KIND_LOCATORS,
    REGISTRY_KINDS,
    REGISTRY_TABLE_LOCATORS,
    EntityUuidRegistry,
    metadata,
)
from doc_store_server.runtime.entity_lifecycle import (
    DEFAULT_FIELDS,
    EntityLifecycleService,
    _registered_table,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "versions"
REGISTRY_MIGRATION = "0005_entity_lifecycle_registry"
# Every migration that shapes entity_uuid_registry, in chain order. 0005 creates it
# with a composite key and no kind; 0018a aligns it with the ObjectRegistry contract.
# The contract is a property of the chain, not of any single revision.
REGISTRY_MIGRATIONS = (
    "0005_entity_lifecycle_registry",
    "0018a_object_registry_kind_alignment",
)

# Locators declared ahead of the table that will back them. G-001/T-004/A-001
# enrolled content_attachment before the attachment branch created its table.
FORWARD_DECLARED_LOCATORS = frozenset({"content_attachments"})

REGISTER_TRIGGER = re.compile(r"CREATE TRIGGER trg_(?P<table>\w+)_register_entity_uuid")
LOCATOR_UPSERT = re.compile(
    r"ON CONFLICT \(entity_id\)\s+DO UPDATE\s+SET entity_table",
    re.IGNORECASE,
)


def _load_migration(name: str) -> ModuleType:
    path = MIGRATIONS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offline_sql(module: ModuleType, function: str = "upgrade") -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        getattr(module, function)()
    return output.getvalue()


def _registry_upgrade_sql() -> str:
    return _offline_sql(_load_migration(REGISTRY_MIGRATION))


def _registry_chain_sql() -> str:
    return "\n".join(_offline_sql(_load_migration(name)) for name in REGISTRY_MIGRATIONS)


def _registration_function_sql(upgrade_sql: str) -> str:
    start = upgrade_sql.index("CREATE OR REPLACE FUNCTION doc_store_register_entity_uuid()")
    end = upgrade_sql.index("CREATE OR REPLACE FUNCTION doc_store_unregister_entity_uuid()", start)
    return upgrade_sql[start:end]


def _registration_migration_sql() -> dict[str, str]:
    """Render every migration that installs registration triggers to offline SQL."""

    rendered: dict[str, str] = {}
    for path in sorted(MIGRATIONS.glob("*.py")):
        if "register_entity_uuid" not in path.read_text():
            continue
        rendered[path.name] = _offline_sql(_load_migration(path.stem))
    assert rendered, "no migration installs registration triggers"
    return rendered


def _check_constraint_sql(name: str) -> str:
    for constraint in EntityUuidRegistry.__table__.constraints:
        if constraint.name == name:
            return str(getattr(constraint, "sqltext"))
    raise AssertionError(f"missing check constraint {name}")


class _StubResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubRegistryConnection:
    """Answers ``SELECT entity_table ... WHERE entity_id = :entity_id`` from a dict."""

    def __init__(self, rows: Mapping[UUID, str]) -> None:
        self._rows = dict(rows)
        self.statements: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: Any, parameters: Mapping[str, Any] | None = None) -> _StubResult:
        bound = dict(parameters or {})
        self.statements.append((str(statement), bound))
        return _StubResult(self._rows.get(bound.get("entity_id")))


@contextlib.contextmanager
def _stub_connect(connection: _StubRegistryConnection) -> Iterator[Any]:
    yield connection


def _service_with(connection: _StubRegistryConnection) -> EntityLifecycleService:
    service = EntityLifecycleService("postgresql://unused/registry-tests")
    setattr(service, "_connect", lambda: _stub_connect(connection))
    return service


def test_registry_resolves_a_uuid_to_one_locator_without_a_caller_supplied_type() -> None:
    """{ss45}: the UUID alone selects the locator; nothing else narrows the row."""

    document_id = uuid4()
    chunk_id = uuid4()
    unregistered_id = uuid4()
    connection = _StubRegistryConnection(
        {document_id: "documents", chunk_id: "semantic_chunks"}
    )

    assert _registered_table(cast(Any, connection), document_id) == "documents"
    assert _registered_table(cast(Any, connection), chunk_id) == "semantic_chunks"
    assert _registered_table(cast(Any, connection), unregistered_id) is None

    for sql, parameters in connection.statements:
        normalised = " ".join(sql.split())
        assert normalised == (
            "SELECT entity_table FROM entity_uuid_registry WHERE entity_id = :entity_id"
        )
        # No caller-supplied type participates in the lookup.
        assert set(parameters) == {"entity_id"}
        assert "entity_table =" not in normalised
        assert "kind" not in normalised


def test_registry_lookup_rejects_a_locator_the_boundary_cannot_serve() -> None:
    """A locator outside the typed tables the runtime knows is an error, not a guess."""

    forward_declared_id = uuid4()
    connection = _StubRegistryConnection({forward_declared_id: "content_attachments"})

    with pytest.raises(ValueError, match="content_attachments"):
        _registered_table(cast(Any, connection), forward_declared_id)


def test_owner_tree_resolves_by_uuid_alone_and_rejects_a_mismatched_type() -> None:
    """The public lookup boundary needs no type, and refuses a type that contradicts it."""

    document_id = uuid4()
    missing_id = uuid4()
    connection = _StubRegistryConnection({document_id: "documents"})
    service = _service_with(connection)

    with pytest.raises(LookupError, match=str(missing_id)):
        service.owner_tree(entity_id=str(missing_id))

    with pytest.raises(ValueError, match="belongs to documents, not projects"):
        service.owner_tree(entity_id=str(document_id), entity_type="project")


def test_registry_row_binds_one_uuid_identity_to_kind_and_locator() -> None:
    """{vi72}/{dl1d}: single-column UUID primary key plus a required kind and locator."""

    table = EntityUuidRegistry.__table__

    assert [column.name for column in table.primary_key.columns] == ["entity_id"]
    assert table.c.entity_id.type.python_type is UUID
    assert table.c.kind.nullable is False
    assert table.c.entity_table.nullable is False
    assert any(
        {column.name for column in constraint.columns} == {"entity_id"}
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )


def test_registry_check_constraints_enumerate_the_whole_allowlist() -> None:
    """The DDL that reaches the database carries the allowlist, not a stale copy of it."""

    kind_sql = _check_constraint_sql("ck_entity_uuid_registry_kind_allowlisted")
    pair_sql = _check_constraint_sql("ck_entity_uuid_registry_kind_locator_allowlisted")

    assert set(re.findall(r"'([a-z_]+)'", kind_sql)) == set(REGISTRY_KINDS)
    assert set(re.findall(r"\('([a-z_]+)', '([a-z_]+)'\)", pair_sql)) == set(
        REGISTRY_KIND_LOCATORS.items()
    )
    assert pair_sql.startswith("(kind, entity_table) IN (")


def test_each_allowlisted_kind_owns_exactly_one_typed_table_locator() -> None:
    """{dl1d}: kinds and locators are one-to-one, and locators name real typed tables."""

    locators = list(REGISTRY_KIND_LOCATORS.values())
    assert len(set(locators)) == len(locators), "two kinds share one typed table"
    assert set(REGISTRY_TABLE_LOCATORS) == set(locators)
    assert set(REGISTRY_KINDS) == set(REGISTRY_KIND_LOCATORS)

    missing = set(locators) - set(metadata.tables)
    assert missing == set(FORWARD_DECLARED_LOCATORS), (
        "every allowlisted locator must name a mapped table, except the ones a later "
        f"branch still owns: unexpected {sorted(missing - FORWARD_DECLARED_LOCATORS)}"
    )


def test_entity_uuid_registry_is_the_only_registry_mapping() -> None:
    """{vq2e}: the registry is extended in place; no parallel registry is declared."""

    registry_tables = {name for name in metadata.tables if "registry" in name}
    assert registry_tables == {"entity_uuid_registry"}
    locator_tables = {
        name
        for name, table in metadata.tables.items()
        if {"entity_table", "entity_id"} <= set(table.c.keys())
    }
    assert locator_tables == {"entity_uuid_registry"}


def test_registration_trigger_rejects_a_registered_uuid_without_upserting_it() -> None:
    """{b2v7}: conflict means refusal; the locator of a registered UUID never changes."""

    function_sql = _registration_function_sql(_registry_upgrade_sql())

    assert "ON CONFLICT (entity_id) DO NOTHING" in function_sql
    assert "DO UPDATE" not in function_sql
    assert not LOCATOR_UPSERT.search(function_sql)
    assert "SET entity_table" not in function_sql
    assert "IF NOT FOUND THEN" in function_sql
    assert "ERRCODE = 'unique_violation'" in function_sql
    # The refusal names the locator that already holds the UUID and the one asking.
    assert "SELECT entity_table INTO registered_table" in function_sql
    assert "|| coalesce(registered_table, 'unknown')" in function_sql
    assert "|| TG_TABLE_NAME" in function_sql


def test_registry_backfill_never_reassigns_an_already_registered_locator() -> None:
    """A re-runnable backfill must not become an upsert on the second run."""

    migration = _load_migration(REGISTRY_MIGRATION)
    upgrade_sql = _registry_upgrade_sql()

    for table in migration.ENTITY_TABLES:
        assert f"FROM {table} ON CONFLICT (entity_id) DO NOTHING" in upgrade_sql
    assert "DO UPDATE" not in upgrade_sql
    assert not LOCATOR_UPSERT.search(upgrade_sql)


def test_preflight_aborts_before_any_ddl_when_one_uuid_has_two_locators() -> None:
    """{tj7i}: legacy cross-table duplicates stop the migration before it changes anything."""

    migration = _load_migration(REGISTRY_MIGRATION)
    upgrade_sql = _registry_upgrade_sql()

    preflight_at = upgrade_sql.index("entity_uuid_registry preflight failed")
    first_ddl_at = min(
        upgrade_sql.index(marker)
        for marker in ("ALTER TABLE ", "CREATE TABLE ", "CREATE INDEX ", "CREATE OR REPLACE ")
        if marker in upgrade_sql
    )
    assert preflight_at < first_ddl_at, "preflight must abort before the first DDL statement"

    preflight_sql = upgrade_sql[:first_ddl_at]
    for table in migration.ENTITY_TABLES:
        assert f"AS entity_table FROM {table}" in preflight_sql
    assert preflight_sql.count("UNION ALL") == len(migration.ENTITY_TABLES) - 1
    assert "GROUP BY entity_id" in preflight_sql
    assert "HAVING count(*) > 1" in preflight_sql
    assert "ERRCODE = 'unique_violation'" in preflight_sql
    # Deterministic report: ordered by UUID, and by locator inside each UUID.
    assert "ORDER BY entity_id" in preflight_sql
    assert "'+' ORDER BY entity_table" in preflight_sql


def test_every_migration_entity_table_stays_registration_consistent() -> None:
    """Each registered typed table is triggered, allowlisted, mapped and servable."""

    migration = _load_migration(REGISTRY_MIGRATION)
    upgrade_sql = _registry_upgrade_sql()
    downgrade_sql = _offline_sql(migration, "downgrade")

    for table in migration.ENTITY_TABLES:
        assert f"CREATE TRIGGER trg_{table}_register_entity_uuid" in upgrade_sql
        assert f"CREATE TRIGGER trg_{table}_unregister_entity_uuid" in upgrade_sql
        assert f"DROP TRIGGER IF EXISTS trg_{table}_register_entity_uuid" in downgrade_sql
        assert table in REGISTRY_TABLE_LOCATORS, f"{table} is registered but not allowlisted"
        assert table in metadata.tables
        assert table in DEFAULT_FIELDS, f"{table} is registered but the lookup cannot serve it"


def test_every_trigger_registered_table_in_any_migration_is_allowlisted() -> None:
    """No migration may register a typed table the allowlist does not know."""

    triggered: set[str] = set()
    for sql in _registration_migration_sql().values():
        triggered.update(match.group("table") for match in REGISTER_TRIGGER.finditer(sql))

    assert triggered, "no registration triggers found in migrations"
    assert triggered <= set(REGISTRY_TABLE_LOCATORS), (
        f"registered without an allowlisted kind: {sorted(triggered - set(REGISTRY_TABLE_LOCATORS))}"
    )
    # Everything the allowlist claims, minus locators whose table a later branch owns,
    # is actually registered by some migration.
    assert set(REGISTRY_TABLE_LOCATORS) - triggered == set(FORWARD_DECLARED_LOCATORS)


def test_migration_chain_leaves_the_registry_the_orm_declares() -> None:
    """The migrated registry must end up as the ObjectRegistry contract declares."""

    sql = _registry_chain_sql()

    assert "ADD COLUMN kind VARCHAR(64)" in sql
    assert "ALTER COLUMN kind SET NOT NULL" in sql
    assert "DROP CONSTRAINT pk_entity_uuid_registry" in sql
    assert "ADD CONSTRAINT pk_entity_uuid_registry PRIMARY KEY (entity_id)" in sql
    assert "ck_entity_uuid_registry_kind_allowlisted" in sql
    assert "ck_entity_uuid_registry_kind_locator_allowlisted" in sql
    # The registration trigger must supply the kind the table now requires.
    assert "INSERT INTO entity_uuid_registry (kind, entity_table, entity_id)" in sql

    # Both sides of the contract, so the test fails if either drifts.
    registry = EntityUuidRegistry.__table__
    assert [column.name for column in registry.primary_key.columns] == ["entity_id"]
    assert "kind" in registry.c


def test_no_migration_reassigns_a_registered_locator_by_upsert() -> None:
    """{b2v7}: registry allocation must never upsert, in any migration."""

    offenders = sorted(
        name for name, sql in _registration_migration_sql().items() if LOCATOR_UPSERT.search(sql)
    )
    assert offenders == [], f"migrations upserting a registry locator: {offenders}"
